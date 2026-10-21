#!/usr/bin/env python3
"""PostToolUse hook: after an agent creates an issue or PR, or merges a PR, ask it to sync related issues.

Reads the hook payload on stdin (Claude Code or Codex). It reacts to `gh issue create`, `gh pr create` and
`gh pr merge` run through the shell tool, and to GitHub MCP/connector tools that create issues or PRs or
merge PRs. When a gh-proman config applies to the target repository, it prints an additionalContext that
tells the agent to run `sync`. In every other case it prints nothing and exits 0. It filters the command
itself, because harnesses that load Claude plugins (Codex, Copilot) ignore the hook's `if` field and run
it after every shell command.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_proman as gp  # noqa: E402

MCP_TOOL = re.compile(r"^mcp__.+__(?:github_)?(create_issue|issue_write|create_pull_request|merge_pull_request)$",
                      re.I)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)
FAILED_TEXT = re.compile(r"(?:exit(?:ed)?(?: with)? (?:status|code)|exit_code)[:= ]+([1-9]\d*)|already exists|"
                         r"^(?:error|fatal|GraphQL):", re.I | re.M)
MERGED_TEXT = re.compile(r"pull request (?:([\w.-]+/[\w.-]+))?#(\d+)", re.I)
OWN_COMMANDS = ("gh_proman.py", "gh-proman", "hook.py")
SHELLS = {"bash", "sh", "zsh", "/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/bash", "/usr/bin/zsh"}
OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", "\n"}
VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--title", "-B", "--base", "-H", "--head",
               "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project", "-r", "--reviewer",
               "-T", "--template", "--subject", "-A", "--author-email", "--match-head-commit", "--recover"}
SEEN_WINDOW = 600
MAX_CONTEXT = 1200


def command_text(tool_input) -> str:
    command = (tool_input or {}).get("command") or (tool_input or {}).get("cmd") or ""
    if isinstance(command, list):
        if len(command) >= 3 and command[0] in SHELLS and command[1] in ("-c", "-lc", "-ic"):
            return command[2]
        return shlex.join(str(c) for c in command)
    return command if isinstance(command, str) else ""


def tokenize(command):
    command = HEREDOC.sub(" ", command)  # heredoc bodies are data, and their quotes break shlex
    try:
        lex = shlex.shlex(re.sub(r"\\\n", " ", command).replace("\n", " ; "), posix=True,
                          punctuation_chars=";&|()")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace splitting, enough to spot `gh <noun> <verb>`.
        return re.findall(r"&&|\|\||[;&|()]|[^\s;&|()]+", command)


def gh_invocations(command, cwd: Path):
    """[(action, args, cwd)] for each `gh issue create|pr create|pr merge` in a shell command."""
    found = []
    tokens = tokenize(command)
    here = cwd
    i = 0
    starts = {0} | {n + 1 for n, t in enumerate(tokens) if t in OPERATORS}
    for n in sorted(starts):
        # Our own CLI (or `python3 .../gh_proman.py`) writes to issues itself: never remind about it.
        lead = tokens[n:n + 2]
        if lead and (os.path.basename(lead[0]) in OWN_COMMANDS or
                     (lead[0].startswith("python") and len(lead) > 1 and os.path.basename(lead[1]) in OWN_COMMANDS)
                     or lead == ["gh", "proman"]):
            return []
    while i < len(tokens):
        tok = tokens[i]
        if tok == "cd" and i + 1 < len(tokens) and tokens[i + 1] not in OPERATORS:
            target = Path(os.path.expanduser(tokens[i + 1]))
            here = target if target.is_absolute() else (here / target)
            i += 2
            continue
        if tok == "gh" or tok.endswith("/gh"):
            j = i + 1
            repo = None
            while j < len(tokens) and tokens[j] in ("-R", "--repo") and j + 1 < len(tokens):
                repo = tokens[j + 1]
                j += 2
            if j < len(tokens) and tokens[j].startswith("--repo="):
                repo = tokens[j].split("=", 1)[1]
                j += 1
            if j + 1 < len(tokens) and (tokens[j], tokens[j + 1]) in (("issue", "create"), ("pr", "create"),
                                                                      ("pr", "merge")):
                action = f"{tokens[j]} {tokens[j + 1]}"
                k = j + 2
                args = []
                while k < len(tokens) and tokens[k] not in OPERATORS:
                    args.append(tokens[k])
                    k += 1
                if repo:
                    args = ["-R", repo, *args]
                found.append((action, args, here))
                i = k
                continue
        i += 1
    return found


def flag(args, *names):
    for idx, arg in enumerate(args):
        for name in names:
            if arg == name and idx + 1 < len(args):
                return args[idx + 1]
            if arg.startswith(name + "=") and name.startswith("--"):
                return arg.split("=", 1)[1]
    return None


def positional(args):
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in VALUE_FLAGS:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from strings(v)


def failed(response) -> bool:
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode", "returncode", "return_code"):
            if isinstance(response.get(key), int) and response[key] != 0:
                return True
        if response.get("is_error") or response.get("isError") or response.get("interrupted"):
            return True
        text = "\n".join(str(response.get(k) or "") for k in ("stderr", "output", "error"))
        return bool(FAILED_TEXT.search(text)) if "stdout" not in response or response.get("stderr") else False
    if isinstance(response, str):
        return bool(FAILED_TEXT.search(response))
    return False


def created_url(response, kind):
    """URL of the item a tool created. Structured responses (MCP) carry it as html_url/url on the object itself;
    URLs inside its body or title belong to other items, so they are only a last resort. gh prints the URL last."""
    want = "pull" if kind == "pr" else "issues"
    queue = [response]
    while queue:
        value = queue.pop(0)
        if isinstance(value, str) and value.strip()[:1] in "{[":
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict):
            for key in ("html_url", "url"):
                m = gp.URL_RE.fullmatch(str(value.get(key) or ""))
                if m and m.group(3) == want:
                    return m.group(0)
            queue += [v for k, v in value.items() if k not in ("body", "title", "description")]
        elif isinstance(value, list):
            queue += value
    found = [m.group(0) for text in strings(response) for m in gp.URL_RE.finditer(text) if m.group(3) == want]
    return found[-1] if found else None


def seen(key, store: Path) -> bool:
    try:
        data = json.loads(store.read_text()) if store.is_file() else {}
    except (OSError, ValueError):
        data = {}
    stamp = time.time()
    data = {k: v for k, v in data.items() if stamp - v < SEEN_WINDOW}
    hit = key in data
    data[key] = stamp
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(data))
    except OSError:
        pass
    return hit


def body_links_issue(text, slug) -> bool:
    return bool(gp.refs_in(text or "", slug))


def detect(payload):
    """The event this tool call produced, or None: {event, url|selector, repo, cwd, body?, auto?}."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    response = payload.get("tool_response")
    cwd = Path(payload.get("cwd") or os.getcwd())

    m = MCP_TOOL.match(tool)
    if m and "github" not in tool.lower():
        return None  # Linear, Jira, GitLab… servers use the same tool names
    if m:
        action = m.group(1)
        if action == "issue_write" and (tool_input.get("method") or "create") != "create":
            return None
        if failed(response):
            return None
        owner, repo = tool_input.get("owner"), tool_input.get("repo")
        slug = f"{owner}/{repo}" if owner and repo else None
        if action == "merge_pull_request":
            number = tool_input.get("pullNumber") or tool_input.get("pull_number") or tool_input.get("number")
            if not (slug and number):
                return None
            return {"event": "pr-merged", "selector": str(number), "repo": slug, "cwd": cwd}
        kind = "pr" if action == "create_pull_request" else "issue"
        url = created_url(response, kind)
        if not url:
            return None
        event = "pr-opened" if kind == "pr" else "issue-created"
        return {"event": event, "url": url, "repo": slug, "cwd": cwd, "body": tool_input.get("body"),
                "body_known": kind == "pr"}

    command = command_text(tool_input)
    if not command:
        return None
    for action, args, here in reversed(gh_invocations(command, cwd)):
        if "--web" in args or "-w" in args or "--dry-run" in args:
            return None
        repo = flag(args, "-R", "--repo")
        if action == "pr merge":
            if failed(response):
                return None
            selector = positional(args)
            merged = [m for t in strings(response) for m in MERGED_TEXT.finditer(t)]
            if not selector and merged:
                selector = f"{merged[-1].group(1)}#{merged[-1].group(2)}" if merged[-1].group(1) \
                    else merged[-1].group(2)
            if not selector:
                # No number: gh merged the current branch's PR. Name the branch now; it may be deleted next.
                branch = gp.Repo(here).git("rev-parse", "--abbrev-ref", "HEAD", check=False)
                selector = branch if branch and branch != "HEAD" else None
            return {"event": "pr-merged", "selector": selector, "repo": repo, "cwd": here,
                    "auto": "--auto" in args}
        kind = "pr" if action == "pr create" else "issue"
        if failed(response):
            return None
        url = created_url(response, kind)
        if not url:
            return None
        info = {"event": "pr-opened" if kind == "pr" else "issue-created", "url": url, "repo": repo,
                "cwd": here}
        if kind == "pr":
            body = flag(args, "-b", "--body")
            body_file = flag(args, "-F", "--body-file")
            if body_file and body_file != "-":
                path = Path(body_file) if Path(body_file).is_absolute() else here / body_file
                try:
                    body = path.read_text(encoding="utf-8")
                except OSError:
                    body = None
            fill = any(a.startswith("--fill") or a == "-f" for a in args)
            info["body"] = body
            info["body_known"] = body is not None and not fill
        return info
    return None


def context_for(info, cfg, script):
    ref = gp.parse_ref(info.get("url") or "")
    branches = ", ".join(b for b in cfg["integration_branches"])
    lang = {"it": "Italian", "en": "English"}[cfg["language"]]
    board = f"{cfg['project']['owner']}/{cfg['project']['number']}"
    if info["event"] == "pr-merged":
        target = info.get("url") or info.get("selector")
        if not target:
            return ("gh-proman: you just merged a PR but its number is unknown. Find it with `gh pr list --state "
                    "merged --limit 1 --json url`, then run "
                    f"`python3 \"{script}\" sync --source <url> --event pr-merged --apply --json`.")
        repo_arg = f" --repo {info['repo']}" if info.get("repo") else ""
        if info.get("auto"):
            return (f"gh-proman: auto-merge was enabled for {target}. Nothing to sync yet; when it actually merges, "
                    f"run `python3 \"{script}\" sync --source {target}{repo_arg} --json` (or a later audit catches it).")
        lines = [f"gh-proman: you just merged PR {target} (board {board}). Sync it now, following the Sync section "
                 "of the gh-proman skill:",
                 f"  python3 \"{script}\" sync --source {target}{repo_arg} --event pr-merged --apply --json",
                 "Then write the `needs_text` comments and judge `needs_judgment` candidates. Close an issue only "
                 f"if the PR completes it and has landed in an integration branch ({branches}), following "
                 "references/rubric.md (`close` or `apply`); otherwise comment with what landed and what remains."]
    else:
        what = "PR" if info["event"] == "pr-opened" else "issue"
        lines = [f"gh-proman: you just opened {what} {ref.short if ref else info.get('url')} (board {board}). "
                 "Sync it now, following the Sync section of the gh-proman skill:",
                 f"  python3 \"{script}\" sync --source {info['url']} --apply --json",
                 "Then write the `needs_text` comments and judge `needs_judgment` candidates (related work, "
                 "duplicates); comment only where it adds context."]
        if info["event"] == "pr-opened":
            lines.append(f"Don't close issues when a PR is opened; closing happens when it merges into {branches}.")
            if info.get("body_known") and not body_links_issue(info.get("body"), ref.slug if ref else None):
                lines.append("The PR body links no issue: add \"Refs owner/repo#N\" (or \"Closes owner/repo#N\" when "
                             "it completes the issue) with `gh pr edit <url> --body-file <file>`.")
    lines.append(f"Write comments in {lang}.")
    text = "\n".join(lines)
    return text[:MAX_CONTEXT]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        if not isinstance(payload, dict) or os.environ.get("GH_PROMAN_DISABLE") == "1":
            return 0
        info = detect(payload)
        if not info:
            return 0
        local = gp.Repo(info["cwd"])
        ref = gp.parse_ref(info.get("url") or "")
        slug = (ref.slug if ref else None) or info.get("repo") or local.slug
        if not slug:
            return 0
        root = local.root if local.slug and local.slug.lower() == slug.lower() else None
        cfg, _ = gp.resolve_config(slug, root)
        if not cfg:
            return 0
        if info["event"] == "pr-merged" and not info.get("url") and info.get("selector"):
            sel = gp.parse_ref(info["selector"], slug, "pr")
            if sel:
                info["url"] = sel.url
        if not info.get("repo"):
            info["repo"] = slug
        key = payload.get("tool_use_id") or f"{info['event']}:{info.get('url') or info.get('selector')}:{slug}"
        store = (local.git_dir() / "gh-proman" / "hook-seen") if local.root else \
            Path.home() / ".cache" / "gh-proman" / "hook-seen"
        if seen(str(key), store) or (info.get("url") and seen(f"{info['event']}:{info['url']}", store)):
            return 0
        context = context_for(info, cfg, gp.SCRIPT)
    except Exception:  # noqa: BLE001 - never disturb the session over a sync reminder
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
