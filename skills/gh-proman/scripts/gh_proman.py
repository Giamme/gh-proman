#!/usr/bin/env python3
"""gh-proman: keep GitHub Project issues in sync with the work agents do.

This is the deterministic half of the gh-proman skill. Every GitHub and git read or write goes through
here, with idempotent markers, safety guards and JSON output. The agent does the judging (which issues
relate, what to write); this script does the I/O. Stdlib only; it shells out to `gh` and `git`.

    python3 gh_proman.py [-C DIR] [--dry-run] [--json] <command> [--help]

Exit codes: 0 ok, 2 error, 3 a guard refused (the JSON on stdout says why and what to do instead).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION = "0.1.0"
SCRIPT = Path(__file__).resolve()
SKILL_DIR = SCRIPT.parents[1]
BODY_LIMIT = 65536
SEP = "\n\n"


class Fail(Exception):
    code = 2

    def __init__(self, message, data=None):
        super().__init__(message)
        self.data = data or {}


class Refused(Fail):
    code = 3


# ---------------------------------------------------------------------------------------------- processes

def child_env() -> dict:
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C", "GH_PAGER": "", "PAGER": "cat", "NO_COLOR": "1", "CLICOLOR": "0",
                "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1"})
    return env


def run(cmd, cwd=None, stdin=None, check=True):
    try:
        proc = subprocess.run(cmd, cwd=cwd, input=stdin.encode("utf-8") if stdin is not None else None,
                              capture_output=True, env=child_env())
    except FileNotFoundError:
        raise Fail(f"`{cmd[0]}` is not installed or not on PATH")
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        detail = (err.strip() or out.strip())[:600]
        raise Fail(f"`{' '.join(str(c) for c in cmd[:3])}` failed: {detail}")
    return proc.returncode, out, err


def now() -> dt.datetime:
    fixed = os.environ.get("GH_PROMAN_NOW")
    if fixed:
        return parse_time(fixed)
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value) -> dt.datetime:
    if not value:
        raise Fail("missing date")
    text = str(value).strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    # Python < 3.11 accepts only 3 or 6 fractional digits.
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise Fail(f"not a date: {value!r} (use YYYY-MM-DD or ISO 8601)")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def fmt_date(moment: dt.datetime, lang: str) -> str:
    # Built by hand: strftime month names and separators follow the shell locale (it_IT on this team).
    if lang == "it":
        return f"{moment.day:02d}/{moment.month:02d}/{moment.year:04d}"
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_agent() -> str:
    if os.environ.get("GH_PROMAN_AGENT"):
        return os.environ["GH_PROMAN_AGENT"]
    if os.environ.get("CLAUDECODE"):
        return "Claude Code"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "Codex"
    return "agent"


# ---------------------------------------------------------------------------------------------- gh client

class Gh:
    """All GitHub access. Mutations are recorded instead of sent when dry_run is set."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.planned = []
        self.mutations = 0
        self._viewer = None

    def _call(self, args, payload=None):
        stdin = None
        if payload is not None:
            args = [*args, "--input", "-"]
            stdin = json.dumps(payload)
        _, out, _ = run(["gh", *args], stdin=stdin)
        out = out.strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise Fail(f"gh returned non-JSON output for `gh {' '.join(args[:2])}`: {out[:200]}")

    def rest(self, path, method="GET", payload=None, paginate=False):
        if method != "GET":
            if self.dry_run:
                self.planned.append({"rest": f"{method} {path}", "payload": payload})
                return None
            self.mutations += 1
        args = ["api", path]
        if method != "GET":
            args += ["-X", method]
        if paginate:
            args += ["--paginate", "--slurp"]
        data = self._call(args, payload)
        if paginate:
            flat = []
            for page in data or []:
                flat.extend(page if isinstance(page, list) else [page])
            return flat
        return data

    def try_rest(self, path):
        try:
            return self.rest(path)
        except Fail:
            return None

    def graphql(self, query, **variables):
        op = re.match(r"\s*(query|mutation)\s+(\w+)", query)
        if op and op.group(1) == "mutation":
            if self.dry_run:
                self.planned.append({"graphql": op.group(2), "variables": variables})
                return None
            self.mutations += 1
        data = self._call(["api", "graphql"], {"query": query, "variables": variables})
        if data and data.get("errors"):
            raise Fail("GitHub GraphQL: " + "; ".join(e.get("message", "") for e in data["errors"]))
        return (data or {}).get("data") or {}

    def viewer(self) -> str:
        if self._viewer is None:
            self._viewer = (self.rest("user") or {}).get("login") or ""
        return self._viewer


# ---------------------------------------------------------------------------------------------- refs

URL_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)")
SHORT_RE = re.compile(r"(?<![\w/.-])([\w-]+)/([\w.-]+)#(\d+)\b")
HASH_RE = re.compile(r"(?<![\w/&#])#(\d+)\b")
COMMIT_URL_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/commit/([0-9a-f]{7,40})")


class Ref:
    def __init__(self, owner, repo, number, kind=None):
        self.owner, self.repo, self.number, self.kind = owner, repo, int(number), kind

    @property
    def slug(self):
        return f"{self.owner}/{self.repo}"

    @property
    def short(self):
        return f"{self.slug}#{self.number}"

    @property
    def url(self):
        return f"https://github.com/{self.slug}/{'pull' if self.kind == 'pr' else 'issues'}/{self.number}"

    def same(self, other) -> bool:
        return other is not None and self.slug.lower() == other.slug.lower() and self.number == other.number

    def __repr__(self):
        return self.short


def parse_ref(text, default_slug=None, kind=None):
    text = (text or "").strip()
    m = URL_RE.fullmatch(text.rstrip("/")) or URL_RE.match(text)
    if m:
        return Ref(m.group(1), m.group(2), m.group(4), "pr" if m.group(3) == "pull" else "issue")
    m = re.fullmatch(r"([\w-]+)/([\w.-]+)#(\d+)", text)
    if m:
        return Ref(m.group(1), m.group(2), m.group(3), kind)
    m = re.fullmatch(r"#?(\d+)", text)
    if m and default_slug:
        owner, repo = default_slug.split("/", 1)
        return Ref(owner, repo, m.group(1), kind)
    return None


def refs_in(text, default_slug=None):
    """Every issue/PR reference in free text: URLs, owner/repo#N, and #N (resolved against default_slug)."""
    found = []
    text = text or ""
    for m in URL_RE.finditer(text):
        found.append(Ref(m.group(1), m.group(2), m.group(4), "pr" if m.group(3) == "pull" else "issue"))
    stripped = URL_RE.sub(" ", text)
    for m in SHORT_RE.finditer(stripped):
        found.append(Ref(m.group(1), m.group(2), m.group(3)))
    if default_slug:
        owner, repo = default_slug.split("/", 1)
        for m in HASH_RE.finditer(SHORT_RE.sub(" ", stripped)):
            found.append(Ref(owner, repo, m.group(1)))
    return found


CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|chiude|chiudi|risolve)\b\s*:?\s+"
                        r"(https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+|(?:[\w-]+/[\w.-]+)?#\d+)", re.I)


def closing_refs_in(text, default_slug=None):
    """References introduced by a closing keyword (Closes/Fixes/Resolves, Chiude/Risolve)."""
    found = []
    for m in CLOSING_RE.finditer(text or ""):
        found += refs_in(m.group(1), default_slug)
    return found


def slug_from_remote(url: str):
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?\s*$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


# ---------------------------------------------------------------------------------------------- git repo

class Repo:
    def __init__(self, path):
        self.root = None
        self.slug = None
        path = Path(path or ".")
        if not path.exists():
            return
        rc, out, _ = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False)
        if rc != 0:
            return
        self.root = Path(out.strip())
        rc, url, _ = run(["git", "-C", str(self.root), "remote", "get-url", "origin"], check=False)
        if rc == 0:
            self.slug = slug_from_remote(url.strip())

    def git(self, *args, check=True):
        if not self.root:
            raise Fail("not inside a git repository (pass -C <dir>)")
        _, out, _ = run(["git", "-c", "core.quotePath=false", "-C", str(self.root), *args], check=check)
        return out.strip()

    def ok(self, *args) -> bool:
        if not self.root:
            return False
        rc, _, _ = run(["git", "-C", str(self.root), *args], check=False)
        return rc == 0

    def git_dir(self) -> Path:
        common = Path(self.git("rev-parse", "--git-common-dir"))
        return common if common.is_absolute() else self.root / common


# ---------------------------------------------------------------------------------------------- config

DEFAULTS = {
    "version": 1,
    "language": "it",
    "project": None,
    "integration_branches": ["@default"],
    "status": {"backlog": "Backlog", "ready": "Ready", "in_progress": "In progress", "in_review": "In review",
               "done": "Done"},
    "status_field": "Status",
    "draft_pr_status": "in_progress",
    "pr_status": "in_review",
    "priority": {"field": "Priority", "options": ["P0", "P1", "P2", "P3"], "default": "P2", "source": "auto",
                 "map": {}},
    "issue_types": {"bug": "Bug", "feature": "Feature", "chore": "Task"},
    "title_format": "[{id} · {priority}] {title}",
    "plain_title_format": "[{priority}] {title}",
    "task_id_pattern": r"\b[A-Z]\d{2}(?:\.\d+)?\b",
    "status_block_position": "top",
    "duplicate_threshold": 0.5,
    "autoclose": {"enabled": True, "max_closes_per_run": 3, "max_actions_per_run": 20,
                  "cross_repo": "explicit-link-only"},
    "related_owners": [],
    "estimate": {"field": "Estimate", "scale": [0.5, 1, 2, 3, 5, 8, 13], "max_hours": 13, "min_samples": 5,
                 "prior_factor": 1.0, "session_gap_minutes": 60, "session_lead_minutes": 15},
}
STATUS_ORDER = ["backlog", "ready", "in_progress", "in_review", "done"]
CONFIG_NAME = ".github/gh-proman.json"


def user_config_path() -> Path:
    if os.environ.get("GH_PROMAN_CONFIG"):
        return Path(os.environ["GH_PROMAN_CONFIG"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "gh-proman" / "config.json"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Fail(f"{path} is not valid JSON: {exc}")


def deep_merge(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_config(slug, root):
    """(config, sources). config is None when nothing applies; the hook stays silent then."""
    if os.environ.get("GH_PROMAN_DISABLE") == "1":
        return None, ["disabled by GH_PROMAN_DISABLE=1"]
    layers, sources = [], []
    upath = user_config_path()
    if upath.is_file() and slug:
        for profile in (read_json(upath) or {}).get("profiles", []):
            globs = profile.get("match") or []
            if any(fnmatch.fnmatch(slug.lower(), g.lower()) for g in globs):
                layers.append({k: v for k, v in profile.items() if k != "match"})
                sources.append(f"{upath} (profile {', '.join(globs)})")
                break
    if root:
        rpath = Path(root) / CONFIG_NAME
        if rpath.is_file():
            layers.append(read_json(rpath) or {})
            sources.append(str(rpath))
    if not layers:
        return None, []
    cfg = DEFAULTS
    for layer in layers:
        cfg = deep_merge(cfg, layer)
    project = cfg.get("project") or {}
    if not project.get("owner") or not project.get("number"):
        raise Fail(f"gh-proman config ({'; '.join(sources)}) has no project.owner / project.number")
    if cfg["language"] not in I18N:
        raise Fail(f"unsupported language {cfg['language']!r} (use one of {', '.join(I18N)})")
    return cfg, sources


# ---------------------------------------------------------------------------------------------- i18n

I18N = {
    "it": {
        "footer": "gh-proman · aggiornamento automatico ({agent}) · {date}",
        "block_title": "Stato (gh-proman)",
        "updated": "aggiornato",
        "prs": "PR",
        "criteria": "Criteri coperti",
        "covered": "Ultimo controllo del codice",
        "pr_state": {"draft": "draft", "open": "aperta", "merged": "unita", "closed": "chiusa"},
        "reopen": "Se la chiusura non è corretta, riapri l'issue: il prossimo sync ne terrà conto.",
        "closing_head": "**Chiusa da gh-proman** ({reason}).",
        "reason": {"completed": "completata", "not_planned": "non pianificata", "duplicate": "duplicata"},
        "criteria_head": "Criteri e prove:",
        "evidence": "Prove",
        "duplicate_of": "Duplicata di {ref}.",
        "estimate": "Stima", "lanes": "lane", "factor": "fattore", "actual": "Tempo effettivo",
        "actual_source": {"reported": "riportato", "git": "dai commit"},
        "priority_desc": {"P0": "Bloccante: subito", "P1": "Alta: prossima iterazione",
                          "P2": "Normale", "P3": "Bassa: quando possibile"},
    },
    "en": {
        "footer": "gh-proman · automatic update ({agent}) · {date}",
        "block_title": "Status (gh-proman)",
        "updated": "updated",
        "prs": "PRs",
        "criteria": "Criteria covered",
        "covered": "Last code check",
        "pr_state": {"draft": "draft", "open": "open", "merged": "merged", "closed": "closed"},
        "reopen": "If this closure is wrong, reopen the issue: the next sync will take it into account.",
        "closing_head": "**Closed by gh-proman** ({reason}).",
        "reason": {"completed": "completed", "not_planned": "not planned", "duplicate": "duplicate"},
        "criteria_head": "Criteria and evidence:",
        "evidence": "Evidence",
        "duplicate_of": "Duplicate of {ref}.",
        "estimate": "Estimate", "lanes": "lanes", "factor": "factor", "actual": "Actual time",
        "actual_source": {"reported": "reported", "git": "from commits"},
        "priority_desc": {"P0": "Blocking: now", "P1": "High: next iteration", "P2": "Normal",
                          "P3": "Low: when possible"},
    },
}


# ---------------------------------------------------------------------------------------------- markers

COMMENT_MARK_RE = re.compile(r"<!-- gh-proman:v1 ((?:[\w-]+=[^\s>]+\s*)+)-->")
OUR_MARK_RE = re.compile(r"<!--\s*gh-proman")
BLOCK_START = "<!-- gh-proman:status:start -->"
BLOCK_END = "<!-- gh-proman:status:end -->"
STATE_RE = re.compile(r"<!-- gh-proman:state (\{.*?\}) -->", re.S)


def make_marker(**kv) -> str:
    parts = [f"{k}={v}" for k, v in kv.items() if v not in (None, "")]
    for part in parts:
        if re.search(r"[\s>]", part):
            raise Fail(f"marker value must not contain spaces or '>': {part!r}")
    return "<!-- gh-proman:v1 " + " ".join(parts) + " -->"


def parse_marker(body):
    m = COMMENT_MARK_RE.search(body or "")
    if not m:
        return None
    return dict(p.split("=", 1) for p in m.group(1).split())


def escape_ours(text: str) -> str:
    """Agent-written text must not be able to forge or break our markers."""
    return OUR_MARK_RE.sub("&lt;!-- gh-proman", text or "")


def find_block(body: str):
    starts = [m.start() for m in re.finditer(re.escape(BLOCK_START), body)]
    ends = [m.start() for m in re.finditer(re.escape(BLOCK_END), body)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise Refused("the issue body has duplicate or unbalanced gh-proman status markers; leave the body alone "
                      "and post a comment instead", {"starts": len(starts), "ends": len(ends)})
    return starts[0], ends[0] + len(BLOCK_END)


def strip_block(body: str) -> str:
    span = find_block(body)
    if not span:
        return body
    start, end = span
    before, after = body[:start], body[end:]
    if start == 0 and after.startswith(SEP):
        after = after[len(SEP):]
    elif end == len(body) and before.endswith(SEP):
        before = before[:-len(SEP)]
    return before + after


def block_state(body: str) -> dict:
    span = find_block(body or "")
    if not span:
        return {}
    m = STATE_RE.search(body[span[0]:span[1]])
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def put_block(body: str, block: str, position: str) -> str:
    span = find_block(body)
    if span:
        return body[:span[0]] + block + body[span[1]:]
    if not body:
        return block
    return block + SEP + body if position == "top" else body + SEP + block


def render_block(state: dict, cfg: dict) -> str:
    lang = cfg["language"]
    t = I18N[lang]
    status_key = state.get("status")
    status_name = cfg["status"].get(status_key, status_key or "?")
    updated = parse_time(state["updated"]) if state.get("updated") else now()
    lines = [BLOCK_START, f"> **{t['block_title']}** · {status_name} · {t['updated']} {fmt_date(updated, lang)}"]
    for line in state.get("summary") or []:
        lines.append("> " + escape_ours(line).replace("\n", " "))
    prs = state.get("prs") or []
    if prs:
        labels = []
        for pr in prs:
            label = t["pr_state"].get(pr.get("state", "open"), pr.get("state", ""))
            labels.append(f"{pr['ref']} ({label} → {pr.get('base', '?')})")
        lines.append(f"> {t['prs']}: " + ", ".join(labels))
    if state.get("criteria"):
        lines.append(f"> {t['criteria']}: {escape_ours(str(state['criteria']))}")
    est = state.get("estimate")
    if est:
        lanes = len(est.get("lanes") or [])
        lines.append(f"> {t['estimate']}: {fmt_hours(est['hours'])} h · {lanes} {t['lanes']} · "
                     f"{t['factor']} {est.get('factor', 1)}")
    act = state.get("actual")
    if act:
        lines.append(f"> {t['actual']}: {fmt_hours(act['hours'])} h "
                     f"({t['actual_source'].get(act.get('source'), act.get('source', ''))})")
    covered = state.get("covered") or {}
    if covered:
        lines.append(f"> {t['covered']}: " + ", ".join(f"{repo}@{v.get('sha', '')[:7]}"
                                                         for repo, v in sorted(covered.items())))
    raw = json.dumps(state, sort_keys=True, ensure_ascii=True, separators=(",", ":")).replace(">", "\\u003e")
    lines += [f"<!-- gh-proman:state {raw} -->", BLOCK_END]
    return "\n".join(lines)


def fmt_hours(value) -> str:
    return f"{float(value):g}"


CHECK_RE = re.compile(r"^\s*[-*+] \[( |x|X)\] (.+?)\s*$", re.M)
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)


def checklist(body: str) -> dict:
    """Checkboxes outside our block, with the heading they sit under."""
    text = strip_block(body or "")
    heads = [(m.start(), m.group(1)) for m in HEADING_RE.finditer(text)]
    items = {"open": [], "done": []}
    for m in CHECK_RE.finditer(text):
        section = ""
        for pos, title in heads:
            if pos < m.start():
                section = title
        entry = {"text": m.group(2).strip(), "section": section}
        items["done" if m.group(1) in "xX" else "open"].append(entry)
    return items


# ---------------------------------------------------------------------------------------------- GraphQL

ISSUE_FIELDS = """id number title body url state stateReason createdAt updatedAt closedAt author { login }
  repository { nameWithOwner }
  closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
    nodes { url number state isDraft baseRefName repository { nameWithOwner } } }"""

Q_PROJECT = """query GhpProject($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) { login ... on ProjectV2Owner { projectV2(number: $number) { id title url
    fields(first: 50) { nodes {
      ... on ProjectV2FieldCommon { id name dataType }
      ... on ProjectV2SingleSelectField { options { id name color description } } } } } } } }"""

Q_ITEMS = """query GhpProjectItems($owner: String!, $number: Int!, $after: String, $status: String!,
    $priority: String!, $estimate: String!) {
  repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectV2(number: $number) {
    items(first: 50, after: $after) { pageInfo { hasNextPage endCursor } nodes { id
      status: fieldValueByName(name: $status) { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      priority: fieldValueByName(name: $priority) { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      estimate: fieldValueByName(name: $estimate) { ... on ProjectV2ItemFieldNumberValue { number } }
      content { __typename ... on Issue { %s } ... on PullRequest { id number title url state isDraft
        repository { nameWithOwner } } } } } } } } }""" % ISSUE_FIELDS

Q_ISSUE = """query GhpIssue($owner: String!, $repo: String!, $number: Int!, $status: String!) {
  repository(owner: $owner, name: $repo) { issue(number: $number) { %s
    projectItems(first: 20) { nodes { id project { id }
      status: fieldValueByName(name: $status) { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }""" \
    % ISSUE_FIELDS

Q_PULL = """query GhpPull($owner: String!, $repo: String!, $number: Int!, $status: String!) {
  repository(owner: $owner, name: $repo) { nameWithOwner defaultBranchRef { name }
    pullRequest(number: $number) { id number title body url state isDraft merged mergedAt createdAt
      headRefName baseRefName author { login } mergeCommit { oid }
      closingIssuesReferences(first: 25) { nodes { url number repository { nameWithOwner } } }
      files(first: 100) { nodes { path } }
      commits(last: 100) { nodes { commit { oid messageHeadline messageBody } } }
      projectItems(first: 20) { nodes { id project { id }
        status: fieldValueByName(name: $status) { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }"""

Q_KIND = """query GhpKind($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) { issueOrPullRequest(number: $number) { __typename } } }"""

Q_MERGED = """query GhpMergedPulls($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequests(states: MERGED, first: 50, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title body url mergedAt updatedAt headRefName baseRefName mergeCommit { oid }
        closingIssuesReferences(first: 25) { nodes { url number repository { nameWithOwner } } }
        files(first: 100) { nodes { path } }
        commits(last: 50) { nodes { commit { oid messageHeadline messageBody } } } } } } }"""

Q_PR_COMMITS = """query GhpPullCommits($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) { pullRequest(number: $number) {
    commits(first: 100) { nodes { commit { authoredDate } } } } } }"""

Q_ORG_FIELDS = """query GhpOrgFields($owner: String!) {
  organization(login: $owner) { issueFields(first: 50) { nodes { __typename
    ... on IssueFieldSingleSelect { id name options { id name } } } } } }"""

Q_ORG_TYPES = """query GhpOrgTypes($owner: String!) {
  organization(login: $owner) { issueTypes(first: 50) { nodes { id name } } } }"""

M_SET_ISSUE_FIELD = """mutation GhpSetIssueField($issue: ID!, $field: ID!, $option: ID!) {
  setIssueFieldValue(input: {issueId: $issue, issueFields: [{fieldId: $field, singleSelectOptionId: $option}]}) {
    issue { id } } }"""

M_ADD_ITEM = """mutation GhpAddItem($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } } }"""
M_SET_FIELD = """mutation GhpSetField($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}}) { projectV2Item { id } } }"""
M_SET_NUMBER = """mutation GhpSetNumber($project: ID!, $item: ID!, $field: ID!, $number: Float!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
    value: {number: $number}}) { projectV2Item { id } } }"""
M_CREATE_NUMBER_FIELD = """mutation GhpCreateNumberField($project: ID!, $name: String!) {
  createProjectV2Field(input: {projectId: $project, dataType: NUMBER, name: $name}) {
    projectV2Field { ... on ProjectV2Field { id } } } }"""
M_CREATE_FIELD = """mutation GhpCreateField($project: ID!, $name: String!,
    $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  createProjectV2Field(input: {projectId: $project, dataType: SINGLE_SELECT, name: $name,
    singleSelectOptions: $options}) { projectV2Field { ... on ProjectV2SingleSelectField { id } } } }"""
M_UPDATE_FIELD = """mutation GhpUpdateField($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
    projectV2Field { ... on ProjectV2SingleSelectField { id options { id name } } } } }"""
M_CLOSE = """mutation GhpCloseIssue($issue: ID!, $reason: IssueClosedStateReason, $duplicate: ID) {
  closeIssue(input: {issueId: $issue, stateReason: $reason, duplicateIssueId: $duplicate}) {
    issue { id state } } }"""
M_SUB_ISSUE = """mutation GhpAddSubIssue($parent: ID!, $child: ID!) {
  addSubIssue(input: {issueId: $parent, subIssueId: $child}) { issue { id } } }"""


# ---------------------------------------------------------------------------------------------- context

class Ctx:
    def __init__(self, args):
        self.args = args
        self.json = bool(getattr(args, "json", False))
        self.gh = Gh(dry_run=bool(getattr(args, "dry_run", False)))
        self.repo = Repo(getattr(args, "C", None) or ".")
        self.agent = getattr(args, "agent", None) or detect_agent()
        self._cfg = None
        self._sources = []
        self._project = None
        self.actions = 0
        self.closes = 0
        self._orgs = {}

    def org_meta(self, owner):
        """Org-level issue fields and issue types. Both need `read:org`; without it GitHub returns no fields and
        refuses the types, so `error` says what's missing instead of pretending the org has none."""
        if owner not in self._orgs:
            meta = {"fields": {}, "types": None, "error": None}
            try:
                data = self.gh.graphql(Q_ORG_FIELDS, owner=owner)
                nodes = (((data.get("organization") or {}).get("issueFields") or {}).get("nodes")) or []
                meta["fields"] = {n["name"]: n for n in nodes if n and n.get("name")}
            except Fail as exc:
                meta["error"] = str(exc)
            try:
                data = self.gh.graphql(Q_ORG_TYPES, owner=owner)
                nodes = (((data.get("organization") or {}).get("issueTypes") or {}).get("nodes")) or []
                meta["types"] = {n["name"].lower(): n for n in nodes if n and n.get("name")}
            except Fail as exc:
                meta["error"] = meta["error"] or str(exc)
            self._orgs[owner] = meta
        return self._orgs[owner]

    @property
    def slug(self):
        return getattr(self.args, "repo", None) or self.repo.slug

    def cfg(self, slug=None):
        if self._cfg is None:
            slug = slug or self.slug
            root = self.repo.root if (self.repo.slug and slug and self.repo.slug.lower() == slug.lower()) else None
            cfg, sources = resolve_config(slug, root)
            if cfg is None:
                hint = " ".join(sources) or (f"no {CONFIG_NAME} in this repo and no matching profile in "
                                             f"{user_config_path()}; run `config init` (ask the user first)")
                raise Fail(f"gh-proman is not configured for {slug or 'this directory'}: {hint}")
            self._cfg, self._sources = cfg, sources
        return self._cfg

    @property
    def lang(self):
        return self.cfg()["language"]

    def project(self):
        if self._project is None:
            p = self.cfg()["project"]
            self._project = Project(self.gh, p["owner"], int(p["number"]), self.cfg())
        return self._project

    def count(self, kind="action"):
        limits = self.cfg()["autoclose"]
        self.actions += 1
        if self.actions > int(limits.get("max_actions_per_run", 20)):
            raise Refused(f"more than {limits.get('max_actions_per_run')} write actions in one run; split the work "
                          "or ask the user")
        if kind == "close":
            self.closes += 1
            if self.closes > int(limits.get("max_closes_per_run", 3)):
                raise Refused(f"more than {limits.get('max_closes_per_run')} closures in one run; ask the user to "
                              "confirm the remaining ones")

    def footer(self):
        t = I18N[self.lang]
        return "<sub>" + t["footer"].format(agent=self.agent, date=fmt_date(now(), self.lang)) + "</sub>"


class Project:
    def __init__(self, gh, owner, number, cfg):
        self.gh, self.owner, self.number, self.cfg = gh, owner, number, cfg
        data = gh.graphql(Q_PROJECT, owner=owner, number=number)
        node = ((data.get("repositoryOwner") or {}).get("projectV2"))
        if not node:
            raise Fail(f"project {owner}/{number} not found or not visible to this token (needs the `project` "
                       "scope: `gh auth refresh -s project`)")
        self.id, self.title, self.url = node["id"], node["title"], node.get("url")
        self.fields = {f["name"]: f for f in node["fields"]["nodes"] if f and f.get("name")}
        self._items = None

    @property
    def label(self):
        return f"{self.owner}/{self.number} \"{self.title}\""

    def field(self, name):
        return self.fields.get(name)

    def option_id(self, field_name, option_name):
        field = self.field(field_name)
        if not field:
            raise Fail(f"project {self.label} has no field {field_name!r}")
        for opt in field.get("options") or []:
            if opt["name"].lower() == option_name.lower():
                return opt["id"]
        raise Fail(f"field {field_name!r} on {self.label} has no option {option_name!r} "
                   f"(has: {', '.join(o['name'] for o in field.get('options') or []) or 'none'}); "
                   "run `project setup` or fix the config")

    def items(self):
        if self._items is None:
            items, after = [], None
            while True:
                data = self.gh.graphql(Q_ITEMS, owner=self.owner, number=self.number, after=after,
                                       status=self.cfg["status_field"], priority=self.cfg["priority"]["field"],
                                       estimate=self.cfg["estimate"]["field"])
                page = data["repositoryOwner"]["projectV2"]["items"]
                for node in page["nodes"]:
                    content = node.get("content") or {}
                    if not content:
                        continue
                    items.append({"item_id": node["id"], "status": (node.get("status") or {}).get("name"),
                                  "priority": (node.get("priority") or {}).get("name"),
                                  "estimate": (node.get("estimate") or {}).get("number"),
                                  "type": content.get("__typename"), **content})
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = page["pageInfo"]["endCursor"]
            self._items = items
        return self._items

    def add(self, content_id):
        data = self.gh.graphql(M_ADD_ITEM, project=self.id, content=content_id)
        return ((data or {}).get("addProjectV2ItemById") or {}).get("item", {}).get("id") or "(dry-run)"

    def set_option(self, item_id, field_name, option_name):
        field = self.field(field_name)
        if not field:
            raise Fail(f"project {self.label} has no field {field_name!r}")
        self.gh.graphql(M_SET_FIELD, project=self.id, item=item_id, field=field["id"],
                        option=self.option_id(field_name, option_name))

    def number_field(self, field_name):
        field = self.field(field_name)
        if not field:
            raise Fail(f"project {self.label} has no field {field_name!r}; run `project setup`")
        if field.get("dataType") != "NUMBER":
            raise Fail(f"field {field_name!r} on {self.label} is {field.get('dataType')}, not a number")
        return field

    def set_number(self, item_id, field_name, value):
        field = self.number_field(field_name)
        self.gh.graphql(M_SET_NUMBER, project=self.id, item=item_id, field=field["id"], number=float(value))


STANDARD_PRIORITIES = [("urgent", "critical", "highest", "p0"), ("high", "p1"), ("medium", "normal", "p2"),
                       ("low", "lowest", "p3")]


def priority_target(ctx, owner):
    """Where Priority lives: ('issue', org issue field) or ('project', project field)."""
    cfg = ctx.cfg()["priority"]
    name, source = cfg["field"], cfg.get("source", "auto")
    if source in ("auto", "issue"):
        meta = ctx.org_meta(owner)
        field = meta["fields"].get(name)
        if field and field.get("__typename", "IssueFieldSingleSelect") == "IssueFieldSingleSelect":
            return "issue", field
        if source == "issue":
            raise Fail(f"no org issue field {name!r} visible on {owner}" +
                       (" (the token needs the read:org scope)" if meta["error"] or not meta["fields"] else ""))
    field = ctx.project().field(name)
    if not field:
        raise Fail(f"project {ctx.project().label} has no field {name!r}; run `project setup`")
    return "project", field


def priority_option(ctx, field, level, where):
    """The field option for P0..P3: config map, then an option named like the level, then the standard
    Urgent/High/Medium/Low names by position."""
    cfg = ctx.cfg()["priority"]
    if level not in cfg["options"]:
        raise Fail(f"priority must be one of {', '.join(cfg['options'])}")
    options = field.get("options") or []
    wanted = (cfg.get("map") or {}).get(level, level)
    for opt in options:
        if opt["name"].lower() == wanted.lower():
            return opt
    idx = cfg["options"].index(level)
    if idx < len(STANDARD_PRIORITIES):
        for opt in options:
            if opt["name"].lower() in STANDARD_PRIORITIES[idx]:
                return opt
    hint = "run `project setup`" if where == "project" else "set priority.map in the config"
    if where == "project" and not options:
        hint += ("; if the board shows Priority values anyway, it's an org issue field and the token needs the "
                 "read:org scope")
    raise Fail(f"no option for {level} in {field.get('name')!r} ({where} field; has: "
               f"{', '.join(o['name'] for o in options) or 'none'}); {hint}")


def apply_priority(ctx, slug, node_id, item_id, level):
    where, field = priority_target(ctx, slug.split("/")[0])
    opt = priority_option(ctx, field, level, where)
    ctx.count()
    if where == "issue":
        ctx.gh.graphql(M_SET_ISSUE_FIELD, issue=node_id, field=field["id"], option=opt["id"])
    else:
        ctx.project().gh.graphql(M_SET_FIELD, project=ctx.project().id, item=item_id, field=field["id"],
                                 option=opt["id"])
    return {"level": level, "option": opt["name"], "field": f"{where}:{field.get('name')}"}


def status_key(cfg, name):
    for key, value in cfg["status"].items():
        if name and value.lower() == name.lower():
            return key
    return None


# ---------------------------------------------------------------------------------------------- loaders

def kind_of(ctx, ref):
    if ref.kind:
        return ref.kind
    data = ctx.gh.graphql(Q_KIND, owner=ref.owner, repo=ref.repo, number=ref.number)
    node = (data.get("repository") or {}).get("issueOrPullRequest")
    if not node:
        raise Fail(f"{ref.short} not found")
    ref.kind = "pr" if node["__typename"] == "PullRequest" else "issue"
    return ref.kind


def load_issue(ctx, ref):
    data = ctx.gh.graphql(Q_ISSUE, owner=ref.owner, repo=ref.repo, number=ref.number,
                          status=ctx.cfg()["status_field"])
    issue = (data.get("repository") or {}).get("issue")
    if not issue:
        raise Fail(f"issue {ref.short} not found")
    ref.kind = "issue"
    return issue


def load_pull(ctx, ref):
    data = ctx.gh.graphql(Q_PULL, owner=ref.owner, repo=ref.repo, number=ref.number,
                          status=ctx.cfg()["status_field"])
    repo = data.get("repository") or {}
    pr = repo.get("pullRequest")
    if not pr:
        raise Fail(f"pull request {ref.short} not found")
    pr["defaultBranch"] = (repo.get("defaultBranchRef") or {}).get("name")
    pr["repository"] = {"nameWithOwner": repo.get("nameWithOwner") or ref.slug}
    ref.kind = "pr"
    return pr


def resolve_source(ctx, text, slug=None):
    slug = slug or ctx.slug
    ref = parse_ref(text, slug)
    if ref:
        return ref
    if not slug:
        raise Fail(f"cannot resolve {text!r} without a repository (pass --repo owner/repo)")
    # A branch name, as `gh pr merge` with no number targets the current branch's PR.
    _, out, _ = run(["gh", "pr", "view", text, "-R", slug, "--json", "url,number"])
    data = json.loads(out)
    return parse_ref(data["url"])


def pr_state(pr):
    if pr.get("merged") or pr.get("state") == "MERGED":
        return "merged"
    if pr.get("state") == "CLOSED":
        return "closed"
    return "draft" if pr.get("isDraft") else "open"


def integration_branches(ctx, slug, default_branch=None):
    out = []
    for name in ctx.cfg()["integration_branches"]:
        if name == "@default":
            if not default_branch:
                default_branch = ((ctx.gh.try_rest(f"repos/{slug}") or {}).get("default_branch"))
            if default_branch:
                out.append(default_branch)
        else:
            out.append(name)
    return list(dict.fromkeys(out))


def contained_in(ctx, slug, sha, branch):
    """True when commit `sha` is reachable from `branch` on GitHub (works without a local checkout)."""
    if not sha:
        return False
    if ctx.repo.slug and ctx.repo.slug.lower() == slug.lower() and ctx.repo.ok("cat-file", "-e", sha):
        for ref in (f"origin/{branch}", branch):
            if ctx.repo.ok("rev-parse", "--verify", "--quiet", ref):
                return ctx.repo.ok("merge-base", "--is-ancestor", sha, ref)
    cmp = ctx.gh.try_rest(f"repos/{slug}/compare/{branch}...{sha}")
    return bool(cmp) and cmp.get("status") in ("behind", "identical")


def landed_in(ctx, pr, slug):
    """Integration branches that contain the merged PR (directly, or later through a stacked base)."""
    if pr_state(pr) != "merged":
        return []
    branches = integration_branches(ctx, slug, pr.get("defaultBranch"))
    if pr.get("baseRefName") in branches:
        return [pr["baseRefName"]]
    sha = (pr.get("mergeCommit") or {}).get("oid")
    return [b for b in branches if contained_in(ctx, slug, sha, b)]


# ---------------------------------------------------------------------------------------------- similarity

STOPWORDS = set("""
the and for with from this that into are was were not but have has had its they them their then than when
what which who will would should could can cannot also only just more most other some such very via per
una uno gli lle del della delle dei degli con che non sono nel nella nelle alla alle allo dal dalla dai come
anche più tra fra sul sulla sui questo questa questi queste quello quella essere solo ogni dopo prima resta
restano viene sono stato stata nei agli all dalle sempre ancora già senza oppure ovvero quando dove cosa
issue issues task tasks fix feat chore test tests todo pr prs http https github com www collitude
""".split())
TOKEN_RE = re.compile(r"[a-zà-ÿ][a-zà-ÿ0-9]{2,}")


def tokens(text):
    text = re.sub(r"https?://\S+", " ", text or "")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).replace("_", " ").replace("-", " ")
    out = []
    for tok in TOKEN_RE.findall(text.lower()):
        if tok in STOPWORDS or re.fullmatch(r"[0-9a-f]{7,40}", tok):
            continue
        out.append(tok)
    return out


def tfidf(tokens_list, idf):
    counts = {}
    for tok in tokens_list:
        counts[tok] = counts.get(tok, 0) + 1
    vec = {tok: (1 + math.log(n)) * idf.get(tok, idf.get("", 1.0)) for tok, n in counts.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {tok: v / norm for tok, v in vec.items()}


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def build_idf(docs):
    df = {}
    for doc in docs:
        for tok in set(doc):
            df[tok] = df.get(tok, 0) + 1
    n = len(docs) + 1
    idf = {tok: math.log(n / (1 + c)) + 1 for tok, c in df.items()}
    idf[""] = math.log(n) + 1
    return idf


BLOB_RE = re.compile(r"github\.com/[\w.-]+/[\w.-]+/blob/[^/\s]+/([^\s#)\]>\"']+)")
TICK_PATH_RE = re.compile(r"`([\w.@-]+(?:/[\w.@\[\]()-]+)+\.\w{1,8})(?::\d+)?`")


def paths_in(text):
    found = set(m.group(1) for m in BLOB_RE.finditer(text or ""))
    found |= set(m.group(1) for m in TICK_PATH_RE.finditer(text or ""))
    return found


def path_overlap(issue_paths, changed):
    hits = set()
    for p in issue_paths:
        for c in changed:
            if c == p or c.endswith("/" + p) or p.endswith("/" + c):
                hits.add(c)
    return sorted(hits)


def task_ids(cfg, text):
    return set(re.findall(cfg["task_id_pattern"], text or ""))


def title_id(cfg, title):
    m = re.match(r"\s*\[([^\]·|]+?)\s*(?:[·|][^\]]*)?\]", title or "")
    if m and re.fullmatch(cfg["task_id_pattern"].replace(r"\b", ""), m.group(1).strip()):
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------------------------- sources

def source_from_pr(ctx, ref):
    pr = load_pull(ctx, ref)
    commits = [{"sha": n["commit"]["oid"], "message": (n["commit"]["messageHeadline"] + "\n" +
                                                       (n["commit"].get("messageBody") or "")).strip()}
               for n in (pr.get("commits") or {}).get("nodes", [])]
    return {
        "kind": "pr", "ref": ref, "url": pr["url"], "node_id": pr["id"], "title": pr["title"],
        "body": pr.get("body") or "", "branch": pr.get("headRefName"), "base": pr.get("baseRefName"),
        "state": pr_state(pr), "created": pr.get("createdAt"), "author": (pr.get("author") or {}).get("login"),
        "closing": [parse_ref(n["url"]) for n in (pr.get("closingIssuesReferences") or {}).get("nodes", [])],
        "files": [n["path"] for n in (pr.get("files") or {}).get("nodes", [])], "commits": commits,
        "merge_sha": (pr.get("mergeCommit") or {}).get("oid"), "raw": pr,
        "project_items": (pr.get("projectItems") or {}).get("nodes", []),
    }


def source_from_issue(ctx, ref):
    issue = load_issue(ctx, ref)
    return {
        "kind": "issue", "ref": ref, "url": issue["url"], "node_id": issue["id"], "title": issue["title"],
        "body": strip_block(issue.get("body") or ""), "branch": None, "base": None,
        "state": issue["state"].lower(), "created": issue.get("createdAt"),
        "author": (issue.get("author") or {}).get("login"), "closing": [], "commits": [],
        "files": sorted(paths_in(issue.get("body") or "")), "raw": issue,
        "project_items": (issue.get("projectItems") or {}).get("nodes", []),
    }


def source_from_range(ctx, rng):
    sep, rec = "\x1f", "\x1e"
    log = ctx.repo.git("log", f"--format={rec}%H{sep}%s{sep}%b{sep}", "--name-only", rng)
    commits, files = [], set()
    for chunk in log.split(rec):
        if not chunk.strip():
            continue
        sha, subject, body, rest = (chunk.split(sep) + ["", "", "", ""])[:4]
        commits.append({"sha": sha.strip(), "message": (subject + "\n" + body).strip()})
        files |= {line.strip() for line in rest.splitlines() if line.strip()}
    return {"kind": "range", "ref": None, "url": None, "title": "", "body": "", "branch": None, "base": None,
            "state": "local", "closing": [], "files": sorted(files), "commits": commits, "range": rng}


def source_from_text(title, body):
    return {"kind": "draft", "ref": None, "url": None, "title": title or "", "body": body or "", "branch": None,
            "base": None, "state": "draft", "closing": [], "files": sorted(paths_in(body)), "commits": []}


def source_text(src, with_body=True):
    parts = [src.get("title") or "", src.get("branch") or ""]
    if with_body:
        parts.append(src.get("body") or "")
    parts += [c["message"] for c in src.get("commits") or []]
    return "\n".join(parts)


# ---------------------------------------------------------------------------------------------- candidates

def candidates(ctx, src, limit=10, mode="related", min_score=0.2, include_closed=None):
    cfg = ctx.cfg()
    items = [i for i in ctx.project().items() if i.get("type") == "Issue"]
    default_slug = src["ref"].slug if src.get("ref") else ctx.slug
    head_text = "\n".join([src.get("title") or "", (src.get("branch") or "").upper()] +
                          [c["message"].split("\n", 1)[0] for c in src.get("commits") or []])
    full_text = source_text(src)
    explicit_refs = refs_in(full_text, default_slug)
    closing_refs = closing_refs_in(full_text, default_slug)
    head_ids = task_ids(cfg, head_text)
    body_ids = task_ids(cfg, src.get("body") or "") - head_ids
    changed = src.get("files") or []
    docs = [tokens(i["title"] + "\n" + strip_block(i.get("body") or "")[:2000]) for i in items]
    idf = build_idf(docs)
    src_vec = tfidf(tokens((src.get("title") or "") + "\n" + (src.get("body") or "")[:2000] + "\n" +
                           "\n".join(c["message"] for c in src.get("commits") or [])), idf)
    src_title_vec = tfidf(tokens(src.get("title") or ""), idf)
    if include_closed is None:
        include_closed = mode == "duplicate"
    out = []
    for item, doc in zip(items, docs):
        ref = parse_ref(item["url"])
        if src.get("ref") and ref.same(src["ref"]):
            continue
        closed = item["state"] != "OPEN"
        if closed and not include_closed:
            continue
        reasons = []
        if any(ref.same(r) for r in src.get("closing") or []):
            reasons.append(("linked", "linked to the PR (Development sidebar or closing keyword)", 1.0))
        if any(ref.same(r) for r in closing_refs):
            reasons.append(("closing-keyword", "introduced by Closes/Fixes/Resolves in the source", 1.0))
        elif any(ref.same(r) for r in explicit_refs):
            reasons.append(("explicit-ref", "mentioned (Refs) in the title, body, branch or commits", 0.9))
        iid = title_id(cfg, item["title"])
        if iid and iid in head_ids:
            reasons.append(("task-id", f"task ID {iid} in the source title, branch or commits", 0.8))
        elif iid and iid in body_ids:
            reasons.append(("task-id-body", f"task ID {iid} mentioned in the source body", 0.45))
        body = item.get("body") or ""
        if src.get("url") and (src["url"] in body or (src["ref"].short in body)):
            reasons.append(("mentioned-by-issue", "the issue body mentions this source", 0.7))
        elif src.get("branch") and len(src["branch"]) > 4 and src["branch"] in body:
            reasons.append(("mentioned-by-issue", f"the issue body mentions branch {src['branch']}", 0.7))
        hits = path_overlap(paths_in(body), changed) if changed else []
        if hits:
            reasons.append(("paths", "touches " + ", ".join(hits[:4]), min(0.6, 0.2 * len(hits))))
        sim = cosine(src_vec, tfidf(doc, idf))
        if sim >= 0.12:
            reasons.append(("text", f"similar wording ({sim:.2f})", round(0.35 * min(1.0, sim * 1.6), 3)))
        if mode == "duplicate":
            tsim = cosine(src_title_vec, tfidf(tokens(item["title"]), idf))
            if tsim >= 0.3:
                reasons.append(("title", f"similar title ({tsim:.2f})", round(0.8 * tsim, 3)))
        if not reasons:
            continue
        score = 1.0
        for _, _, w in reasons:
            score *= (1 - w)
        score = 1 - score
        if closed:
            score *= 0.5
        if score < min_score:
            continue
        out.append({
            "url": item["url"], "ref": ref.short, "repo": ref.slug, "number": ref.number, "title": item["title"],
            "state": item["state"], "status": item.get("status"), "priority": item.get("priority"),
            "item_id": item["item_id"], "score": round(score, 3),
            "link": link_strength(reasons),
            "explicit": link_strength(reasons) is not None,
            "reasons": [{"signal": s, "detail": d, "weight": w} for s, d, w in reasons],
            "checklist": checklist(body),
            "linked_prs": [{"ref": parse_ref(p["url"]).short, "state": pr_state(p), "base": p.get("baseRefName")}
                           for p in (item.get("closedByPullRequestsReferences") or {}).get("nodes", [])],
            "status_block": block_state(body),
            "created": item.get("createdAt"), "author": (item.get("author") or {}).get("login"),
        })
    out.sort(key=lambda c: (-c["score"], c["repo"], c["number"]))
    return out[:limit]


def link_strength(reasons):
    """'closes' (sidebar link, closing keyword, task ID in title/branch), 'refs' (plain mention) or None."""
    signals = {r[0] for r in reasons}
    if signals & {"linked", "closing-keyword", "task-id"}:
        return "closes"
    if "explicit-ref" in signals:
        return "refs"
    return None


def last_markers(ctx, ref):
    comments = ctx.gh.rest(f"repos/{ref.slug}/issues/{ref.number}/comments?per_page=100", paginate=True) or []
    marks = []
    for c in comments:
        m = parse_marker(c.get("body"))
        if m:
            marks.append({**m, "author": (c.get("user") or {}).get("login"), "date": c.get("created_at"),
                          "id": c.get("id")})
    return comments, marks


# ---------------------------------------------------------------------------------------------- writers

def comment_upsert(ctx, ref, event, source, text, sha=None, key=None):
    if not text or not text.strip():
        raise Fail("comment text is empty")
    ctx.count()
    if event == "audit" and not key and sha:
        key = sha[:12]  # each audited head gets its own comment; re-running on the same head is a no-op
    marker = make_marker(event=event, source=source, key=key, sha=sha)
    body = marker + "\n" + escape_ours(text.strip()) + "\n\n" + ctx.footer()
    comments, _ = last_markers(ctx, ref)
    me = ctx.gh.viewer()
    for c in comments:
        m = parse_marker(c.get("body"))
        if not m or (c.get("user") or {}).get("login") != me:
            continue
        if (m.get("event"), m.get("source"), m.get("key")) != (event, source, key):
            continue
        if strip_footer(c["body"]) == strip_footer(body):
            return {"action": "noop", "comment_url": c.get("html_url"), "issue": ref.short}
        ctx.gh.rest(f"repos/{ref.slug}/issues/comments/{c['id']}", "PATCH", {"body": body})
        return {"action": "updated", "comment_url": c.get("html_url"), "issue": ref.short}
    data = ctx.gh.rest(f"repos/{ref.slug}/issues/{ref.number}/comments", "POST", {"body": body})
    return {"action": "created", "comment_url": (data or {}).get("html_url"), "issue": ref.short}


def strip_footer(body):
    return re.sub(r"\n*<sub>gh-proman · [^\n]*</sub>\s*$", "", body or "").strip()


def pr_entry(ctx, pr_ref):
    pr = load_pull(ctx, pr_ref)
    entry = {"ref": pr_ref.short, "state": pr_state(pr), "base": pr.get("baseRefName")}
    if entry["state"] == "merged":
        entry["landed"] = landed_in(ctx, pr, pr_ref.slug)
    return entry


def status_block_upsert(ctx, ref, status=None, summary=None, prs=(), covered=None, criteria=None, estimate=None,
                        actual=None, retries=2):
    ctx.count()
    pr_entries = [pr_entry(ctx, parse_ref(p, ref.slug, "pr")) for p in prs]
    for _ in range(retries + 1):
        issue = ctx.gh.rest(f"repos/{ref.slug}/issues/{ref.number}")
        body = (issue or {}).get("body") or ""
        state = block_state(body) or {"v": 1}
        if status:
            if status not in STATUS_ORDER:
                raise Fail(f"unknown status {status!r} (use one of {', '.join(STATUS_ORDER)})")
            state["status"] = status
        if summary is not None:
            state["summary"] = [s for s in summary if s.strip()]
        known = {p["ref"]: p for p in state.get("prs") or []}
        for entry in pr_entries:
            known[entry["ref"]] = entry
        if known:
            state["prs"] = [known[k] for k in sorted(known)]
        if covered:
            state.setdefault("covered", {})[covered["repo"]] = {"sha": covered["sha"], "date": iso(now())}
        if criteria:
            state["criteria"] = escape_ours(str(criteria)).replace("-->", "--&gt;")
        if estimate:
            state["estimate"] = estimate
        if actual:
            state["actual"] = actual
        state["updated"] = iso(now())
        new = put_block(body, render_block(state, ctx.cfg()), ctx.cfg()["status_block_position"])
        if strip_block(new) != strip_block(body):
            raise Fail("internal error: the status block update would change text outside gh-proman markers")
        if len(new) > BODY_LIMIT:
            raise Refused(f"the issue body would exceed GitHub's {BODY_LIMIT} character limit; post a comment "
                          "instead")
        if same_block(body, new):
            return {"action": "noop", "issue": ref.short, "state": state}
        if ctx.gh.dry_run:
            ctx.gh.planned.append({"rest": f"PATCH repos/{ref.slug}/issues/{ref.number}", "status_block": state})
            return {"action": "would-update", "issue": ref.short, "state": state}
        fresh = ctx.gh.rest(f"repos/{ref.slug}/issues/{ref.number}")
        if (fresh or {}).get("updated_at") != issue.get("updated_at") or (fresh or {}).get("body") != issue.get("body"):
            continue  # someone edited the body meanwhile: recompute from the fresh copy
        ctx.gh.rest(f"repos/{ref.slug}/issues/{ref.number}", "PATCH", {"body": new})
        check = ctx.gh.rest(f"repos/{ref.slug}/issues/{ref.number}")
        if strip_block((check or {}).get("body") or "") != strip_block(body):
            raise Fail(f"after updating {ref.short} the text outside the gh-proman block differs from before; "
                       "check the issue history")
        return {"action": "updated", "issue": ref.short, "state": state, "before_len": len(body),
                "after_len": len(new)}
    raise Refused(f"{ref.short} kept changing while gh-proman tried to update it; post a comment instead")


def same_block(old, new):
    """Equal apart from the 'updated' timestamp, so re-runs with nothing new are no-ops."""
    def norm(text):
        text = re.sub(r'"updated":"[^"]*"', "", text)
        return re.sub(r"(· (?:aggiornato|updated) )\S+", r"\1", text)
    return norm(old) == norm(new)


def find_item(ctx, node_id, project_items=None):
    project = ctx.project()
    for node in project_items or []:
        if (node.get("project") or {}).get("id") == project.id:
            return node["id"], (node.get("status") or {}).get("name")
    return None, None


def ensure_on_board(ctx, ref, node_id=None, project_items=None, status=None, priority=None):
    """Add the issue/PR to the configured board if missing; set Status (forward only) and Priority if unset."""
    project = ctx.project()
    result = {"issue": ref.short, "project": project.label}
    if node_id is None or project_items is None:
        if kind_of(ctx, ref) == "pr":
            raw = load_pull(ctx, ref)
        else:
            raw = load_issue(ctx, ref)
        node_id, project_items = raw["id"], (raw.get("projectItems") or {}).get("nodes", [])
    item_id, current = find_item(ctx, node_id, project_items)
    if not item_id:
        ctx.count()
        item_id = project.add(node_id)
        result["added"] = True
    result["item_id"] = item_id
    if status:
        result["status"] = move_status(ctx, item_id, current, status)
    if priority:
        result["priority"] = apply_priority(ctx, ref.slug, node_id, item_id, priority)
    return result


def move_status(ctx, item_id, current_name, target, allow_backward=False):
    cfg = ctx.cfg()
    if target not in STATUS_ORDER:
        raise Fail(f"unknown status {target!r} (use one of {', '.join(STATUS_ORDER)})")
    cur = status_key(cfg, current_name)
    if cur == target:
        return {"action": "noop", "status": cfg["status"][target]}
    if cur and STATUS_ORDER.index(target) < STATUS_ORDER.index(cur) and not allow_backward:
        return {"action": "skipped", "reason": f"would move backwards from {current_name} to {cfg['status'][target]}"}
    ctx.count()
    ctx.project().set_option(item_id, cfg["status_field"], cfg["status"][target])
    return {"action": "set", "from": current_name, "to": cfg["status"][target]}


def set_status(ctx, ref, target, allow_backward=False):
    raw = load_pull(ctx, ref) if kind_of(ctx, ref) == "pr" else load_issue(ctx, ref)
    items = (raw.get("projectItems") or {}).get("nodes", [])
    item_id, current = find_item(ctx, raw["id"], items)
    added = False
    if not item_id:
        ctx.count()
        item_id = ctx.project().add(raw["id"])
        added = True
    res = move_status(ctx, item_id, current, target, allow_backward)
    return {"issue": ref.short, "added": added, **res}


def set_priority(ctx, ref, priority):
    cfg = ctx.cfg()
    if priority not in cfg["priority"]["options"]:
        raise Fail(f"priority must be one of {', '.join(cfg['priority']['options'])}")
    issue = load_issue(ctx, ref)
    item_id, _ = find_item(ctx, issue["id"], (issue.get("projectItems") or {}).get("nodes", []))
    if not item_id:
        ctx.count()
        item_id = ctx.project().add(issue["id"])
    applied = apply_priority(ctx, ref.slug, issue["id"], item_id, priority)
    title = issue["title"]
    new_title = re.sub(r"^(\[(?:[^\]·]+·\s*)?)P\d(\s*\])", lambda m: m.group(1) + priority + m.group(2), title)
    if new_title != title:
        ctx.count()
        ctx.gh.rest(f"repos/{ref.slug}/issues/{ref.number}", "PATCH", {"title": new_title})
    return {"issue": ref.short, "priority": applied, "title": new_title}


# ---------------------------------------------------------------------------------------------- close

def explicit_link(ctx, issue_ref, pr, issue_title=None):
    closing = [parse_ref(n["url"]) for n in (pr.get("closingIssuesReferences") or {}).get("nodes", [])]
    if any(issue_ref.same(r) for r in closing):
        return "linked"
    slug = (pr.get("repository") or {}).get("nameWithOwner")
    text = "\n".join([pr.get("title") or "", pr.get("body") or "", pr.get("headRefName") or ""] +
                     [(n["commit"]["messageHeadline"] + "\n" + (n["commit"].get("messageBody") or ""))
                      for n in (pr.get("commits") or {}).get("nodes", [])])
    if any(issue_ref.same(r) for r in closing_refs_in(text, slug)):
        return "closing-keyword"
    iid = title_id(ctx.cfg(), issue_title) if issue_title else None
    if iid and iid in task_ids(ctx.cfg(), (pr.get("title") or "") + "\n" + (pr.get("headRefName") or "").upper()):
        return "task-id"
    return None


def close_issue(ctx, ref, reason, comment, evidence=(), criteria_map=None, duplicate_of=None, ack_activity=False,
                source=None):
    cfg = ctx.cfg()
    if not cfg["autoclose"].get("enabled", True):
        raise Refused("auto-close is disabled in the gh-proman config; comment instead and let a person close it")
    if reason not in ("completed", "not_planned", "duplicate"):
        raise Fail("reason must be completed, not_planned or duplicate")
    if not comment or not comment.strip():
        raise Fail("a closing comment is required: say what was done and where")
    issue = load_issue(ctx, ref)
    if issue["state"] != "OPEN":
        return {"action": "noop", "issue": ref.short, "reason": "already closed"}
    project = ctx.project()
    item_id, current = find_item(ctx, issue["id"], (issue.get("projectItems") or {}).get("nodes", []))
    on_board = bool(item_id)
    problems, landed_evidence = [], []
    comments, _ = last_markers(ctx, ref)
    me = ctx.gh.viewer()

    if not ack_activity:
        recent = [c for c in comments if (c.get("user") or {}).get("login") != me
                  and now() - parse_time(c["created_at"]) < dt.timedelta(hours=24)]
        if recent:
            problems.append(f"{len(recent)} comment(s) from others in the last 24 hours: read them, then pass "
                            "--ack-activity if they don't change the decision")

    dup = None
    if reason == "duplicate":
        dup = parse_ref(duplicate_of or "", ref.slug, "issue")
        if not dup:
            raise Fail("--duplicate-of <issue> is required with --reason duplicate")
        original = load_issue(ctx, dup)
        if parse_time(issue["createdAt"]) <= parse_time(original["createdAt"]):
            problems.append(f"{ref.short} is older than {dup.short}; only the newer issue is closed as duplicate")
        if (issue.get("author") or {}).get("login") != me:
            problems.append(f"{ref.short} was opened by someone else; comment on both issues instead")
        if any((c.get("user") or {}).get("login") != me for c in comments):
            problems.append(f"{ref.short} has comments from others; comment on both issues instead")
        dup_id = original["id"]
    elif reason == "completed":
        if not evidence:
            problems.append("--evidence <merged PR or commit URL> is required for a completed closure")
        for ev in evidence:
            commit = COMMIT_URL_RE.match(ev)
            pr_ref = parse_ref(ev, ref.slug, "pr")
            if commit:
                slug, sha = f"{commit.group(1)}/{commit.group(2)}", commit.group(3)
                branches = integration_branches(ctx, slug)
                hit = [b for b in branches if contained_in(ctx, slug, sha, b)]
                if not hit:
                    problems.append(f"commit {sha[:7]} is not on an integration branch ({', '.join(branches)})")
                    continue
                info = ctx.gh.try_rest(f"repos/{slug}/commits/{sha}") or {}
                message = ((info.get("commit") or {}).get("message")) or ""
                if any(ref.same(r) for r in closing_refs_in(message, slug)):
                    landed_evidence.append({"evidence": ev, "landed": hit, "link": "closing-keyword"})
                else:
                    problems.append(f"commit {sha[:7]} has no Closes/Fixes {ref.short} in its message")
            elif pr_ref:
                pr = load_pull(ctx, pr_ref)
                if pr_state(pr) != "merged":
                    problems.append(f"{pr_ref.short} is not merged")
                    continue
                hit = landed_in(ctx, pr, pr_ref.slug)
                if not hit:
                    problems.append(f"{pr_ref.short} merged into {pr.get('baseRefName')}, which has not reached an "
                                    "integration branch yet (stacked PR): comment, don't close")
                    continue
                link = explicit_link(ctx, ref, pr, issue["title"])
                if not link:
                    problems.append(f"{pr_ref.short} has no closing link to {ref.short} (Development sidebar, "
                                    "Closes/Fixes keyword, or its task ID in the PR title/branch); a plain mention "
                                    "or a heuristic match only earns a comment")
                    continue
                if pr_ref.slug.lower() != ref.slug.lower() and not on_board:
                    problems.append(f"{ref.short} is in another repo and not on {project.label}: comment only")
                    continue
                landed_evidence.append({"evidence": pr_ref.short, "landed": hit, "link": link})
            else:
                problems.append(f"evidence {ev!r} is neither a PR nor a commit URL")
        tracked = block_state(issue.get("body") or "").get("prs") or []
        for p in tracked:
            if (p.get("state") != "merged" or not p.get("landed")) and \
                    not any(p["ref"] == e["evidence"] for e in landed_evidence):
                fresh = pr_entry(ctx, parse_ref(p["ref"], ref.slug, "pr"))
                if fresh["state"] != "merged" or not fresh.get("landed"):
                    problems.append(f"{p['ref']}, tracked in the status block, has not landed yet")

    open_items = checklist(issue.get("body") or "")["open"]
    criteria_map = criteria_map or {}
    mapped = []
    for idx, item in enumerate(open_items, 1):
        ev = criteria_map.get(item["text"]) or criteria_map.get(str(idx))
        if ev and str(ev).strip():
            mapped.append((item, str(ev).strip()))
        elif reason == "completed":
            # Only a completed issue must prove its checklist; not planned / duplicate are judgments, not delivery.
            problems.append(f"open checklist item without evidence: {item['text'][:120]!r}")
    if problems:
        raise Refused(f"not closing {ref.short}: " + "; ".join(problems),
                      {"issue": ref.short, "problems": problems, "open_items": open_items})

    ctx.count("close")
    t = I18N[ctx.lang]
    lines = [t["closing_head"].format(reason=t["reason"][reason]), "", comment.strip()]
    if dup:
        lines += ["", t["duplicate_of"].format(ref=dup.short)]
    if mapped:
        lines += ["", t["criteria_head"]] + [f"- [x] {item['text']} — {ev}" for item, ev in mapped]
    if landed_evidence:
        lines += ["", f"{t['evidence']}: " + ", ".join(f"{e['evidence']} → {', '.join(e['landed'])}"
                                                      for e in landed_evidence)]
    lines += ["", t["reopen"]]
    src = source or (landed_evidence[0]["evidence"] if landed_evidence else ref.short)
    res = {"issue": ref.short, "reason": reason}
    res["comment"] = comment_upsert(ctx, ref, "closed", src.replace(" ", ""), "\n".join(lines))
    ctx.gh.graphql(M_CLOSE, issue=issue["id"], reason=reason.upper(), duplicate=dup_id if dup else None)
    res["action"] = "would-close" if ctx.gh.dry_run else "closed"
    if not item_id:
        item_id = project.add(issue["id"])
    res["status"] = move_status(ctx, item_id, current, "done")
    try:
        crit = f"{len(mapped)}/{len(open_items)}" if open_items else None
        res["status_block"] = status_block_upsert(ctx, ref, status="done", criteria=crit)
    except Refused as exc:
        res["status_block"] = {"action": "skipped", "reason": str(exc)}
    return res


# ---------------------------------------------------------------------------------------------- lint & templates

TEMPLATE_DIR = SKILL_DIR / "assets" / "templates"
OPTIONAL_SECTIONS = {"riferimenti", "references", "fuori perimetro", "out of scope"}
SECRET_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9-]{20,}|"
                       r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
BRANCH_BLOB_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/blob/(?![0-9a-f]{40}/)([^/\s]+)/")


def template_sections(lang, kind):
    path = TEMPLATE_DIR / lang / f"{kind}.md"
    if not path.is_file():
        raise Fail(f"no template for {kind!r} in {lang!r}")
    return [m.group(1) for m in re.finditer(r"^## (.+)$", path.read_text(encoding="utf-8"), re.M)]


def lint_body(body, kind, lang):
    errors, warnings = [], []
    for m in BRANCH_BLOB_RE.finditer(body):
        errors.append(f"permalink points at branch {m.group(1)!r}; use a commit SHA (`permalink` command)")
    if SECRET_RE.search(body):
        errors.append("the body contains something that looks like a secret/token")
    if kind:
        heads = {h.strip().lower() for h in re.findall(r"^#{2,3} (.+)$", body, re.M)}
        for section in template_sections(lang, kind):
            if section.lower() not in OPTIONAL_SECTIONS and section.lower() not in heads:
                warnings.append(f"missing section: ## {section}")
    if not CHECK_RE.search(body):
        warnings.append("no `- [ ]` checklist item")
    if re.search(r"<!--(?!\s*gh-proman).*?-->", body, re.S):
        warnings.append("template hint comments (<!-- ... -->) are still in the body")
    if len(body) > BODY_LIMIT:
        errors.append(f"body is longer than {BODY_LIMIT} characters")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------------------------- commands

def read_text_arg(args, text_attr, file_attr):
    path = getattr(args, file_attr, None)
    if path:
        return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return getattr(args, text_attr, None)


def emit(ctx, data, human=None):
    if ctx.gh.dry_run and isinstance(data, dict):
        data = {**data, "dry_run": True, "planned": ctx.gh.planned}
    if ctx.json or human is None:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(human(data))


def cmd_doctor(ctx, args):
    checks = []

    def check(name, fn):
        try:
            ok, detail, fix = fn()
        except Fail as exc:
            ok, detail, fix = False, str(exc), None
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})
        return ok

    check("python", lambda: (sys.version_info >= (3, 9), sys.version.split()[0], "Python 3.9+ is required"))

    def gh_version():
        _, out, _ = run(["gh", "--version"])
        line = out.splitlines()[0] if out else ""
        m = re.search(r"(\d+)\.(\d+)", line)
        ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 48)
        return ok, line, "upgrade gh to 2.48+ (needs `gh api --slurp`)"
    check("gh", gh_version)

    logged_in = []

    def auth():
        rc, out, err = run(["gh", "api", "user", "--jq", ".login"], check=False)
        if rc == 0:
            logged_in.append(out.strip())
        env_token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        if rc != 0:
            fix = ("GITHUB_TOKEN/GH_TOKEN in this shell is invalid and overrides the keyring login: unset it or "
                   "replace it with a token that has the repo and project scopes") if env_token else "gh auth login"
            return False, (err or out).strip()[:200], fix
        _, sout, serr = run(["gh", "auth", "status"], check=False)
        text = sout + serr
        active = re.split(r"\n\s*(?=✓|X)", text)
        block = next((b for b in active if "Active account: true" in b), text)
        scopes = re.search(r"Token scopes: (.*)", block)
        scope_set = set(re.findall(r"'([\w:]+)'", scopes.group(1))) if scopes else set()
        missing = [s for s in ("repo", "project", "read:org") if scope_set and s not in scope_set]
        source = "env GITHUB_TOKEN/GH_TOKEN" if env_token else "keyring"
        detail = f"{out.strip()} via {source}; scopes: {', '.join(sorted(scope_set)) or 'unknown (fine-grained?)'}"
        fix = None
        if missing:
            fix = (f"missing scopes {', '.join(missing)} (read:org is needed for org issue fields and types): " +
                   ("add them to the env token or unset it (gh auth refresh only fixes the keyring login)"
                    if env_token else "gh auth refresh -s project,read:org"))
        return not missing, detail, fix
    check("auth", auth)

    check("repo", lambda: (bool(ctx.repo.root), f"{ctx.repo.root} ({ctx.repo.slug or 'no GitHub origin'})",
                           "run inside the repository or pass -C <dir>"))

    def config():
        cfg = ctx.cfg()
        return True, f"{'; '.join(ctx._sources)} → project {cfg['project']['owner']}/{cfg['project']['number']}, " \
                     f"language {cfg['language']}", None
    configured = check("config", config)

    if configured and logged_in:
        def project():
            p = ctx.project()
            return True, f"{p.label} ({len(p.fields)} fields)", None
        if check("project", project):
            cfg, p = ctx.cfg(), ctx.project()

            def status_field():
                f = p.field(cfg["status_field"])
                names = [o["name"] for o in (f or {}).get("options") or []]
                missing = [v for v in cfg["status"].values() if v not in names]
                return not missing, f"{cfg['status_field']}: {', '.join(names) or 'missing'}", \
                    (f"add the options {', '.join(missing)} in the project settings, or map them in the config "
                     "`status`" if missing else None)
            check("status field", status_field)

            def priority_field():
                owner = (ctx.repo.slug or f"{p.owner}/x").split("/")[0]
                where, f = priority_target(ctx, owner)
                mapping, missing = {}, []
                for lvl in cfg["priority"]["options"]:
                    try:
                        mapping[lvl] = priority_option(ctx, f, lvl, where)["name"]
                    except Fail:
                        missing.append(lvl)
                detail = f"{where} field {f.get('name')}: " + ", ".join(f"{k}→{v}" for k, v in mapping.items())
                if missing:
                    detail += f"; no option for {', '.join(missing)}"
                fix = None
                if missing:
                    fix = ("run `project setup` (ask the user first)" if where == "project" else
                           "map the levels to the org field's options with priority.map in the config")
                    if where == "project" and not f.get("options") and ctx.org_meta(owner)["error"]:
                        fix += "; or, if Priority is an org issue field, add the read:org scope to the token"
                return not missing, detail, fix
            check("priority field", priority_field)

            def estimate_field():
                name = cfg["estimate"]["field"]
                f = p.field(name)
                if not f:
                    return True, f"{name}: missing (estimates are off until `project setup` creates it)", None
                return f.get("dataType") == "NUMBER", f"{name}: {f.get('dataType')}", \
                    f"{name!r} must be a Number field: rename it, or point estimate.field at a Number field"
            check("estimate field", estimate_field)

        if ctx.repo.slug:
            def branches():
                names = integration_branches(ctx, ctx.repo.slug)
                present = [b for b in names if ctx.gh.try_rest(f"repos/{ctx.repo.slug}/branches/{b}")]
                missing = [b for b in names if b not in present]
                detail = ", ".join(present) + (f" (not in this repo: {', '.join(missing)})" if missing else "")
                return bool(present), detail, "none of the integration_branches exist in this repo"
            check("integration branches", branches)

    ok = all(c["ok"] for c in checks)

    def human(data):
        lines = [f"{'ok ' if c['ok'] else 'FAIL'} {c['name']:<20} {c['detail']}" +
                 (f"\n     fix: {c['fix']}" if c["fix"] and not c["ok"] else "") for c in data["checks"]]
        return "\n".join(lines)
    emit(ctx, {"ok": ok, "checks": checks}, human)
    return 0 if ok else 1


def cmd_config(ctx, args):
    if args.action == "show":
        cfg = ctx.cfg()
        emit(ctx, {"sources": ctx._sources, "config": cfg})
        return 0
    if not args.owner or not args.number:
        raise Fail("config init needs --owner and --number (the project board)")
    payload = {"version": 1, "language": args.language, "project": {"owner": args.owner, "number": args.number}}
    if args.branches:
        payload["integration_branches"] = [b.strip() for b in args.branches.split(",") if b.strip()]
    if args.user:
        path = user_config_path()
        data = read_json(path) if path.is_file() else {"profiles": []}
        match = args.match or ([f"{args.owner}/*"])
        for profile in data.get("profiles", []):
            if profile.get("match") == match and not args.force:
                raise Refused(f"{path} already has a profile for {', '.join(match)} (pass --force to replace it)")
        data["profiles"] = [p for p in data.get("profiles", []) if p.get("match") != match]
        data["profiles"].append({"match": match, **payload})
    else:
        if not ctx.repo.root:
            raise Fail("not in a git repository; pass -C <repo> or use --user")
        path = ctx.repo.root / CONFIG_NAME
        if path.exists() and not args.force:
            raise Refused(f"{path} already exists (pass --force to overwrite)")
        data = payload
    if ctx.gh.dry_run:
        emit(ctx, {"would_write": str(path), "content": data})
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    emit(ctx, {"written": str(path), "content": data})
    return 0


PRIORITY_COLORS = {"P0": "RED", "P1": "ORANGE", "P2": "YELLOW", "P3": "GRAY"}


def cmd_project_setup(ctx, args):
    cfg, p = ctx.cfg(), ctx.project()
    t = I18N[ctx.lang]
    report = {"project": p.label, "created": [], "added_options": [], "missing_status": []}
    where, ifield = priority_target(ctx, p.owner) if cfg["priority"].get("source", "auto") != "project" \
        else ("project", None)
    if where == "issue":
        mapping = {lvl: priority_option(ctx, ifield, lvl, "issue")["name"] for lvl in cfg["priority"]["options"]}
        report["priority"] = {"field": f"org issue field {ifield['name']}", "map": mapping,
                              "note": "Priority is an organization issue field; nothing to create on the board"}
        wanted = []
    else:
        wanted = cfg["priority"]["options"]
    new_opts = [{"name": n, "color": PRIORITY_COLORS.get(n, "GRAY"), "description": t["priority_desc"].get(n, "")}
                for n in wanted]
    field = p.field(cfg["priority"]["field"]) if wanted else {"dataType": "SINGLE_SELECT", "options": []}
    if not field:
        p.gh.graphql(M_CREATE_FIELD, project=p.id, name=cfg["priority"]["field"], options=new_opts)
        report["created"].append(cfg["priority"]["field"])
        report["added_options"] = wanted
    else:
        if field.get("dataType") != "SINGLE_SELECT":
            raise Fail(f"field {field['name']!r} exists but is {field.get('dataType')}, not single select")
        existing = field.get("options") or []
        have = {o["name"] for o in existing}
        missing = [o for o in new_opts if o["name"] not in have]
        if missing and wanted:
            # The mutation replaces the whole option list; sending existing ids keeps current values intact.
            keep = [{"id": o["id"], "name": o["name"], "color": o.get("color") or "GRAY",
                     "description": o.get("description") or ""} for o in existing]
            p.gh.graphql(M_UPDATE_FIELD, field=field["id"], options=keep + missing)
            report["added_options"] = [o["name"] for o in missing]
    est_name = cfg["estimate"]["field"]
    est_field = p.field(est_name)
    if not est_field:
        p.gh.graphql(M_CREATE_NUMBER_FIELD, project=p.id, name=est_name)
        report["created"].append(est_name)
    elif est_field.get("dataType") != "NUMBER":
        report["estimate_warning"] = f"{est_name!r} is {est_field.get('dataType')}, not a number; estimates need a " \
                                     "Number field (set estimate.field)"
    status = p.field(cfg["status_field"])
    names = {o["name"] for o in (status or {}).get("options") or []}
    report["missing_status"] = [v for v in cfg["status"].values() if v not in names]
    emit(ctx, report)
    return 0


def cmd_permalink(ctx, args):
    m = re.fullmatch(r"(.+?)(?::(\d+)(?:-(\d+))?)?", args.path)
    path, start, end = m.group(1), m.group(2), m.group(3)
    sha = ctx.repo.git("rev-parse", args.ref)
    if not ctx.repo.git("branch", "-r", "--contains", sha, check=False):
        raise Refused(f"{sha[:7]} is not on any pushed branch, so the link would 404; push first or use an older ref")
    rel = ctx.repo.git("ls-files", "--full-name", "--", path) or path
    url = f"https://github.com/{ctx.repo.slug}/blob/{sha}/{rel}"
    if start:
        url += f"#L{start}" + (f"-L{end}" if end else "")
    emit(ctx, {"url": url}, lambda d: d["url"])
    return 0


def cmd_lint(ctx, args):
    body = read_text_arg(args, "body", "body_file") or ""
    res = lint_body(body, args.type, args.language or ctx.lang)
    emit(ctx, res)
    return 0 if res["ok"] else 3


def load_any_source(ctx, args):
    if getattr(args, "pr", None):
        return source_from_pr(ctx, resolve_source(ctx, args.pr))
    if getattr(args, "issue", None):
        return source_from_issue(ctx, parse_ref(args.issue, ctx.slug, "issue"))
    if getattr(args, "range", None):
        return source_from_range(ctx, args.range)
    if getattr(args, "title", None) or getattr(args, "body_file", None):
        return source_from_text(args.title, read_text_arg(args, "body", "body_file"))
    raise Fail("give one of --pr, --issue, --range or --title/--body-file")


def cmd_candidates(ctx, args):
    src = load_any_source(ctx, args)
    cands = candidates(ctx, src, limit=args.limit, mode=args.mode)
    emit(ctx, {"source": {"kind": src["kind"], "ref": src["ref"].short if src.get("ref") else None,
                          "title": src.get("title")}, "candidates": cands})
    return 0


def format_title(cfg, title, priority, task_id):
    if task_id:
        return cfg["title_format"].format(id=task_id, priority=priority, title=title)
    if cfg.get("plain_title_format") and not title.lstrip().startswith("["):  # keep a prefix the caller wrote
        return cfg["plain_title_format"].format(priority=priority, title=title)
    return title


def create_issue(ctx, slug, kind, title, body, priority=None, task_id=None, status="backlog", parent=None,
                 confirm_new=False, dup_title=None):
    """Create an issue on the board. Raises Refused (with the candidates) when it looks like a duplicate."""
    cfg = ctx.cfg()
    priority = priority or cfg["priority"]["default"]
    if priority not in cfg["priority"]["options"]:
        raise Fail(f"priority must be one of {', '.join(cfg['priority']['options'])}")
    lint = lint_body(body, kind, ctx.lang)
    if not lint["ok"]:
        raise Refused("the body failed lint: " + "; ".join(lint["errors"]), {"lint": lint})
    full_title = format_title(cfg, title, priority, task_id)
    project = ctx.project()
    # Fail before creating anything.
    where, pfield = priority_target(ctx, slug.split("/")[0])
    priority_option(ctx, pfield, priority, where)
    project.option_id(cfg["status_field"], cfg["status"][status])
    warnings = []
    type_name = (cfg.get("issue_types") or {}).get(kind)
    if type_name:
        meta = ctx.org_meta(slug.split("/")[0])
        if meta["types"] is None:
            warnings.append(f"issue type not set: org issue types are not readable ({meta['error'] or 'no access'}"
                            "; the token needs read:org)")
            type_name = None
        elif type_name.lower() not in meta["types"]:
            warnings.append(f"issue type {type_name!r} does not exist in {slug.split('/')[0]}; not set")
            type_name = None
        else:
            type_name = meta["types"][type_name.lower()]["name"]
    if not confirm_new:
        dups = candidates(ctx, source_from_text(dup_title or title, body), limit=5, mode="duplicate", min_score=0.3)
        threshold = float(cfg.get("duplicate_threshold", 0.5))
        if dups and dups[0]["score"] >= threshold:
            raise Refused("possible duplicate", {"threshold": threshold, "candidates": dups,
                                                 "next": "show these to the user; comment on/extend an existing "
                                                         "issue, or re-run with --confirm-new if it really is new"})
    ctx.count()
    payload = {"title": full_title, "body": body}
    if type_name:
        payload["type"] = type_name
    data = ctx.gh.rest(f"repos/{slug}/issues", "POST", payload)
    if ctx.gh.dry_run:
        return {"would_create": {"repo": slug, "title": full_title, "type": type_name, "priority": priority},
                "lint": lint, "warnings": warnings}
    ref = Ref(*slug.split("/", 1), data["number"], "issue")
    res = {"url": data["html_url"], "ref": ref.short, "title": full_title, "type": type_name,
           "lint_warnings": lint["warnings"], "warnings": warnings}
    res["board"] = ensure_on_board(ctx, ref, node_id=data["node_id"], project_items=[], status=status,
                                   priority=priority)
    if parent:
        parent_issue = load_issue(ctx, parse_ref(parent, slug, "issue"))
        ctx.count()
        ctx.gh.graphql(M_SUB_ISSUE, parent=parent_issue["id"], child=data["node_id"])
        res["parent"] = parent_issue["url"]
    return res


def cmd_create(ctx, args):
    slug = args.repo or ctx.slug
    if not slug:
        raise Fail("pass --repo owner/repo")
    body = read_text_arg(args, "body", "body_file") or ""
    emit(ctx, create_issue(ctx, slug, args.type, args.title, body, args.priority, args.id, args.status, args.parent,
                           args.confirm_new))
    return 0


def cmd_comment(ctx, args):
    ref = parse_ref(args.issue, ctx.slug, "issue")
    text = read_text_arg(args, "body", "body_file")
    emit(ctx, comment_upsert(ctx, ref, args.event, args.source, text, sha=args.sha, key=args.key))
    return 0


def cmd_status_block(ctx, args):
    ref = parse_ref(args.issue, ctx.slug, "issue")
    summary = None
    if args.summary is not None or args.summary_file:
        text = read_text_arg(args, "summary_text", "summary_file") if args.summary_file else "\n".join(args.summary)
        summary = [line.strip() for line in text.splitlines() if line.strip()]
    covered = None
    if args.covered:
        repo, _, sha = args.covered.rpartition("=")
        covered = {"repo": repo or ctx.slug, "sha": sha}
    emit(ctx, status_block_upsert(ctx, ref, status=args.status, summary=summary, prs=args.pr or [],
                                  covered=covered, criteria=args.criteria))
    return 0


def cmd_set_status(ctx, args):
    ref = parse_ref(args.issue, ctx.slug)
    emit(ctx, set_status(ctx, ref, args.status, args.allow_backward))
    return 0


def cmd_set_priority(ctx, args):
    emit(ctx, set_priority(ctx, parse_ref(args.issue, ctx.slug, "issue"), args.priority))
    return 0


def cmd_close(ctx, args):
    ref = parse_ref(args.issue, ctx.slug, "issue")
    comment = read_text_arg(args, "comment", "comment_file")
    cmap = json.loads(Path(args.criteria_map).read_text(encoding="utf-8")) if args.criteria_map else {}
    emit(ctx, close_issue(ctx, ref, args.reason, comment, args.evidence or [], cmap, args.duplicate_of,
                          args.ack_activity, args.source))
    return 0


# ---------------------------------------------------------------------------------------------- review findings

FINDING_RE = re.compile(r"^\s*[-*]\s*\[(P[0-3])\]\s*\[(CONFIRMED|PLAUSIBLE)\]\s*(\S.*?)\s+[—–]+\s+(.+?)\s*$", re.M)
# forge files issues itself when gh-proman isn't set up (`forge issues`), with the same fingerprint.
FP_MARK_RE = re.compile(r"<!-- (?:gh-proman|forge):finding fp=([0-9a-f]{12})")
DONE_STATUSES = {"PASS", "MERGED"}

FINDINGS_TEXT = {
    "it": {
        "plausible_prefix": "Da verificare: ",
        "context_tolerated": "Segnalazione della review di forge rimasta aperta: era sotto la soglia bloccante {threshold}, "
                             "quindi il lavoro è stato integrato senza correggerla.",
        "context_review": "Segnalazione della review complessiva di forge rimasta aperta: verificare se è già stata "
                          "corretta prima di lavorarci.",
        "context_plausible": "Segnalazione PLAUSIBILE della review di forge (confidenza bassa): va verificata prima di "
                             "correggere. Se risulta infondata, chiudi l'issue spiegando perché.",
        "origin": "Origine: forge `{plan}`, task `{task}`, review di `{reviewer}`, commit `{commit}`.",
        "steps": "Come riprodurre",
        "finding": "Segnalazione",
        "tasks": ["Riprodurre il caso descritto", "Correggere", "Aggiungere il test di regressione"],
        "tasks_plausible": ["Verificare se il problema è reale (riprodurlo o smentirlo)",
                            "Se reale: correggere e aggiungere il test di regressione"],
        "closing": ["Il caso descritto non si riproduce più"],
        "closing_plausible": ["Il problema è corretto, oppure smentito con motivazione in un commento"],
        "tests": ["Test di regressione che fallisce prima della correzione e passa dopo"],
        "headings": ["Contesto", "Come riprodurre", "Attività", "Criteri di chiusura", "Test e verifica",
                     "Riferimenti"],
        "group_title": "Segnalazioni forge non bloccanti: {scope}",
        "group_context": "Segnalazioni della review di forge rimaste aperte nel run `{plan}` ({n}).",
        "code": "Codice",
        "unpublished": "(commit non ancora pubblicato su GitHub)",
    },
    "en": {
        "plausible_prefix": "To verify: ",
        "context_tolerated": "Forge review finding left open: it was below the blocking threshold {threshold}, so the "
                             "work was merged without fixing it.",
        "context_review": "Forge whole-run review finding left open: check whether it was already fixed before "
                          "working on it.",
        "context_plausible": "PLAUSIBLE forge review finding (low confidence): verify it before fixing. If it turns out "
                             "to be wrong, close the issue saying why.",
        "origin": "Origin: forge `{plan}`, task `{task}`, reviewed by `{reviewer}`, commit `{commit}`.",
        "steps": "Steps to reproduce",
        "finding": "Finding",
        "tasks": ["Reproduce the described case", "Fix it", "Add the regression test"],
        "tasks_plausible": ["Check whether the problem is real (reproduce or refute it)",
                            "If real: fix it and add the regression test"],
        "closing": ["The described case no longer reproduces"],
        "closing_plausible": ["The problem is fixed, or refuted with the reasoning in a comment"],
        "tests": ["Regression test that fails before the fix and passes after"],
        "headings": ["Context", "Steps to reproduce", "Tasks", "Closing criteria", "Tests and verification",
                     "References"],
        "group_title": "Non-blocking forge findings: {scope}",
        "group_context": "Forge review findings left open in run `{plan}` ({n}).",
        "code": "Code",
        "unpublished": "(commit not pushed to GitHub yet)",
    },
}


def _read(path: Path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def reviewer_spec(task_dir: Path):
    kv = dict(line.split("=", 1) for line in _read(task_dir / "qa.resolved").splitlines() if "=" in line)
    spec = ":".join(x for x in (kv.get("alias"), kv.get("effort"), kv.get("harness")) if x)
    return spec or "?"


def clean_markdown(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"(\*\*|__|`)", "", text).strip()


def parse_findings(text):
    out = []
    for m in FINDING_RE.finditer(text or ""):
        loc = m.group(3).strip()
        link = re.fullmatch(r"\[([^\]]+)\]\([^)]*\)", loc)
        loc = (link.group(1) if link else loc).strip("` ")
        lm = re.match(r"(.+?):(\d+)(?:[-:]\d+)?$", loc)
        raw = f"[{m.group(1)}][{m.group(2)}] {loc} — {m.group(4).strip()}"
        out.append({"severity": m.group(1), "confidence": m.group(2), "file": lm.group(1) if lm else loc,
                    "line": int(lm.group(2)) if lm else None, "text": clean_markdown(m.group(4)), "raw": raw})
    return out


def task_findings(task_dir: Path, plan_dir, include_plausible, reviewers):
    """Tolerated (known_issues.md) and, optionally, PLAUSIBLE (qa.last) findings of a task that passed."""
    status = _read(task_dir / "status").split()[:1]
    status = status[0] if status else ""
    results = {}
    if plan_dir and (plan_dir / "results.tsv").is_file():
        for line in _read(plan_dir / "results.tsv").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2:
                results[cols[0]] = cols[1]
    task = task_dir.name
    state = results.get(task, status)
    standalone = not status and task not in results and not (task_dir / "known_issues.md").exists()
    if standalone:
        # A review with no task behind it (whole-run/final review): what it found is still open.
        found = []
        for f in parse_findings(_read(task_dir / "qa.last")):
            if f["confidence"] == "CONFIRMED" or include_plausible:
                found.append({**f, "origin": "review" if f["confidence"] == "CONFIRMED" else "plausible",
                              "task": task, "threshold": "-", "commit": _read(task_dir / "reviewed.commit"),
                              "reviewer": reviewer_spec(task_dir)})
        return found, None
    if state not in DONE_STATUSES and status not in DONE_STATUSES:
        return [], f"{task}: status {state or 'unknown'}, its findings still block it"
    threshold = _read(task_dir / "qa_threshold") or (_read(plan_dir / "qa_threshold") if plan_dir else "")
    commit = _read(task_dir / "merged") or _read(task_dir / "reviewed.commit")
    reviewer = reviewers.get(task) or reviewer_spec(task_dir)
    found = []
    for f in parse_findings(_read(task_dir / "known_issues.md")):
        if f["confidence"] == "CONFIRMED":
            found.append({**f, "origin": "tolerated"})
    if include_plausible:
        for f in parse_findings(_read(task_dir / "qa.last")):
            if f["confidence"] == "PLAUSIBLE":
                found.append({**f, "origin": "plausible"})
    for f in found:
        f.update({"task": task, "threshold": threshold or "?", "commit": commit, "reviewer": reviewer})
    return found, None


def collect_findings(path: Path, include_plausible):
    path = path.resolve()
    notes, findings = [], []
    if path.is_file():
        plan_dir = None
        tasks = [path.parent]
        if path.name == "known_issues.md" or path.name == "qa.last":
            items = parse_findings(_read(path))
            for f in items:
                if f["confidence"] == "CONFIRMED" or include_plausible:
                    f.update({"origin": "tolerated" if f["confidence"] == "CONFIRMED" else "plausible",
                              "task": path.parent.name, "threshold": _read(path.parent / "qa_threshold") or "?",
                              "commit": _read(path.parent / "merged") or _read(path.parent / "reviewed.commit"),
                              "reviewer": reviewer_spec(path.parent)})
                    findings.append(f)
            return {"plan": path.parent.name, "plan_dir": None, "repo_path": None}, findings, notes
        raise Fail(f"{path}: expected a forge plan or task directory, known_issues.md or qa.last")
    if (path / "tasks").is_dir():
        plan_dir = path
        tasks = sorted(d for d in (path / "tasks").iterdir() if d.is_dir() and not d.name.startswith("_"))
    elif (path / "qa.last").is_file() or (path / "known_issues.md").is_file():
        if path.parent.name == "tasks":
            plan_dir = path.parent.parent
        elif (path.parent / "tasks").is_dir():
            plan_dir = path.parent  # e.g. <plan>/final-review
        else:
            plan_dir = None
        tasks = [path]
    else:
        raise Fail(f"{path} is not a forge run: no tasks/, qa.last or known_issues.md")
    reviewers = {}
    if plan_dir and (plan_dir / "tasks.tsv").is_file():
        for line in _read(plan_dir / "tasks.tsv").splitlines():
            cols = line.split("\t")
            if len(cols) >= 6 and not line.startswith("#"):
                reviewers[cols[0]] = cols[5]
    for task_dir in tasks:
        items, note = task_findings(task_dir, plan_dir, include_plausible, reviewers)
        findings += items
        if note:
            notes.append(note)
    repo_path = _read(plan_dir / "repo") if plan_dir else ""
    meta = {"plan": (plan_dir or path).name, "plan_dir": str(plan_dir) if plan_dir else None,
            "repo_path": repo_path or None}
    return meta, findings, notes


def finding_fp(slug, f):
    norm = re.sub(r"\s+", " ", f["text"].lower())[:160]
    return hashlib.sha1(f"{slug.lower()}|{f['file']}|{norm}".encode("utf-8")).hexdigest()[:12]


def finding_title(f, lang):
    first = re.split(r"(?<=[.!?])\s", f["text"], maxsplit=1)[0].rstrip(". ")
    if len(first) > 90:
        first = first[:87].rsplit(" ", 1)[0] + "…"
    title = f"{os.path.basename(f['file'])}: {first}"
    if f["origin"] == "plausible":
        title = FINDINGS_TEXT[lang]["plausible_prefix"] + title
    return title


def finding_link(ctx, slug, repo, f):
    if not f.get("commit") or not f.get("line") and not f.get("file"):
        return None, False
    published = bool(repo.root) and bool(repo.git("branch", "-r", "--contains", f["commit"], check=False))
    if not published:
        cmp = ctx.gh.try_rest(f"repos/{slug}/commits/{f['commit']}")
        published = bool(cmp and cmp.get("sha"))
    url = f"https://github.com/{slug}/blob/{f['commit']}/{f['file']}" + (f"#L{f['line']}" if f.get("line") else "")
    return url, published


def finding_body(ctx, slug, repo, plan, items):
    t = FINDINGS_TEXT[ctx.lang]
    h = t["headings"]
    plausible = all(f["origin"] == "plausible" for f in items)
    first = items[0]
    if len(items) == 1:
        context = t["context_plausible"] if plausible else (
            t["context_review"] if first["origin"] == "review" else
            t["context_tolerated"].format(threshold=first["threshold"]))
    else:
        context = t["group_context"].format(plan=plan, n=len(items))
    origin = t["origin"].format(plan=plan, task=first["task"], reviewer=first["reviewer"],
                                commit=(first.get("commit") or "?")[:12])
    if not first.get("commit"):
        origin = re.sub(r", commit `\?`", "", origin)
    lines = [f"## {h[0]}", context, "", origin, "", f"## {h[1]}"]
    for f in items:
        lines.append(f"> {escape_ours(f['raw'])}")
        lines.append("")
    lines.append(f"## {h[2]}")
    if len(items) == 1:
        lines += [f"- [ ] {x}" for x in (t["tasks_plausible"] if plausible else t["tasks"])]
    else:
        lines += [f"- [ ] [{f['severity']}] `{f['file']}{':' + str(f['line']) if f['line'] else ''}`: "
                  f"{escape_ours(f['text'])[:160]}" for f in items]
    lines += ["", f"## {h[3]}"] + [f"- [ ] {x}" for x in (t["closing_plausible"] if plausible else t["closing"])]
    lines += ["", f"## {h[4]}"] + [f"- [ ] {x}" for x in t["tests"]]
    lines += ["", f"## {h[5]}"]
    for f in items:
        url, published = finding_link(ctx, slug, repo, f)
        loc = f"{f['file']}{':' + str(f['line']) if f['line'] else ''}"
        if url and published:
            lines.append(f"- {t['code']}: {url}")
        else:
            lines.append(f"- {t['code']}: `{loc}` {t['unpublished'] if url else ''}".rstrip())
    lines.append("")
    lines += [f"<!-- gh-proman:finding fp={f['fp']} source=forge:{plan}/{f['task']} -->" for f in items]
    return "\n".join(lines)


SEVERITY_ORDER = ["P0", "P1", "P2", "P3"]


def cmd_findings(ctx, args):
    cfg = ctx.cfg()
    meta, found, notes = collect_findings(Path(args.source), args.include_plausible)
    repo = Repo(meta["repo_path"]) if meta.get("repo_path") else ctx.repo
    slug = args.repo or repo.slug or ctx.slug
    if not slug:
        raise Fail("cannot tell which repository the findings belong to; pass --repo owner/repo")
    if args.min_severity:
        cut = SEVERITY_ORDER.index(args.min_severity)
        found = [f for f in found if SEVERITY_ORDER.index(f["severity"]) <= cut]
    filed = {}
    for item in ctx.project().items():
        for fp in FP_MARK_RE.findall(item.get("body") or ""):
            filed[fp] = {"ref": parse_ref(item["url"]).short, "state": item["state"]}
    # Issues filed before the repo joined the board (e.g. by `forge issues`) aren't board items.
    for issue in ctx.gh.rest(f"repos/{slug}/issues?state=all&per_page=100", paginate=True) or []:
        if "pull_request" in issue:
            continue
        for fp in FP_MARK_RE.findall(issue.get("body") or ""):
            filed.setdefault(fp, {"ref": f"{slug}#{issue['number']}", "state": str(issue.get("state", "")).upper()})
    seen, unique = set(), []
    for f in found:
        f["fp"] = finding_fp(slug, f)
        if f["fp"] in seen:
            continue
        seen.add(f["fp"])
        f["already_filed"] = filed.get(f["fp"])
        unique.append(f)
    todo = [f for f in unique if not f["already_filed"]]
    groups = []
    if args.group == "finding":
        groups = [[f] for f in todo]
    else:
        keyed = {}
        for f in todo:
            key = (f["origin"], f["task"] if args.group == "task" else meta["plan"])
            keyed.setdefault(key, []).append(f)
        groups = list(keyed.values())
    drafts = []
    t = FINDINGS_TEXT[ctx.lang]
    for items in groups:
        items.sort(key=lambda f: SEVERITY_ORDER.index(f["severity"]))
        if len(items) == 1:
            title = finding_title(items[0], ctx.lang)
        else:
            scope = items[0]["task"] if args.group == "task" else meta["plan"]
            title = t["group_title"].format(scope=scope)
            if items[0]["origin"] == "plausible":
                title = t["plausible_prefix"] + title
        draft = {"title": title, "priority": items[0]["severity"], "type": "bug",
                 "fingerprints": [f["fp"] for f in items], "findings": [f["raw"] for f in items],
                 "origin": items[0]["origin"], "body": finding_body(ctx, slug, repo, meta["plan"], items)}
        dups = candidates(ctx, source_from_text(items[0]["text"], items[0]["text"] + " " + items[0]["file"]),
                          limit=3, mode="duplicate", min_score=0.3)
        threshold = float(cfg.get("duplicate_threshold", 0.5))
        draft["possible_duplicates"] = [{"ref": c["ref"], "title": c["title"], "score": c["score"]}
                                        for c in dups if c["score"] >= threshold]
        drafts.append(draft)
    out = {"source": meta, "repo": slug, "notes": notes,
           "summary": {"findings": len(unique), "already_filed": len(unique) - len(todo), "drafts": len(drafts),
                       "tolerated": sum(1 for f in todo if f["origin"] == "tolerated"),
                       "review": sum(1 for f in todo if f["origin"] == "review"),
                       "plausible": sum(1 for f in todo if f["origin"] == "plausible")},
           "already_filed": [{"finding": f["raw"], **f["already_filed"]} for f in unique if f["already_filed"]],
           "drafts": drafts}
    if args.apply:
        created = []
        for draft in drafts[:args.max_issues]:
            if draft["possible_duplicates"] and not args.confirm_new:
                created.append({"title": draft["title"], "skipped": "possible duplicate",
                                "candidates": draft["possible_duplicates"]})
                continue
            ctx.actions = 0  # the per-run cap guards sync sprees; findings have their own --max-issues
            try:
                res = create_issue(ctx, slug, "bug", draft["title"], draft["body"], draft["priority"],
                                   confirm_new=True)
                created.append({"title": draft["title"], **res})
            except Refused as exc:
                created.append({"title": draft["title"], "skipped": str(exc)})
        if len(drafts) > args.max_issues:
            out["notes"].append(f"only the first {args.max_issues} of {len(drafts)} drafts were created "
                                "(--max-issues); run again for the rest")
        out["created"] = created
        for d in out["drafts"]:
            d.pop("body", None)
    elif not args.show_bodies:
        for d in out["drafts"]:
            d["body_preview"] = d.pop("body")[:400]
    emit(ctx, out)
    return 0


# ---------------------------------------------------------------------------------------------- sync / apply

def build_sync(ctx, source, event=None, slug=None):
    cfg = ctx.cfg()
    ref = resolve_source(ctx, source, slug)
    kind = kind_of(ctx, ref)
    src = source_from_pr(ctx, ref) if kind == "pr" else source_from_issue(ctx, ref)
    if event is None:
        event = "issue-created" if kind == "issue" else ("pr-merged" if src["state"] == "merged" else "pr-opened")
    if event.startswith("pr-") and kind != "pr":
        raise Fail(f"{ref.short} is an issue, not a pull request")
    if event == "pr-merged" and src["state"] != "merged":
        raise Refused(f"{ref.short} is not merged (state: {src['state']}); use --event pr-opened")
    plan = {"source": {"ref": ref.short, "url": src["url"], "kind": kind, "title": src["title"],
                       "state": src["state"], "base": src.get("base"), "branch": src.get("branch")},
            "event": event, "language": cfg["language"], "project": ctx.project().label,
            "actions": [], "needs_text": [], "proposed_closes": [], "needs_judgment": [], "nudges": []}
    landed = []
    if kind == "pr":
        landed = landed_in(ctx, src["raw"], ref.slug) if src["state"] == "merged" else []
        plan["source"]["landed_in"] = landed
        plan["integration_branches"] = integration_branches(ctx, ref.slug, src["raw"].get("defaultBranch"))
    item_id, _ = find_item(ctx, src["node_id"], src["project_items"])
    if not item_id:
        board_status = "backlog" if kind == "issue" else (
            "done" if src["state"] == "merged" else (cfg["draft_pr_status"] if src["state"] == "draft"
                                                     else cfg["pr_status"]))
        plan["actions"].append({"op": "add-to-board", "issue": ref.short, "status": board_status,
                                **({"priority": cfg["priority"]["default"]} if kind == "issue" else {})})

    mode = "duplicate" if event == "issue-created" else "related"
    cands = candidates(ctx, src, limit=12, mode=mode, min_score=0.25)
    for c in cands:
        if c["link"] == "refs" and kind == "pr" and c["state"] == "OPEN":
            plan["needs_text"].append({"op": "comment", "issue": c["ref"], "event": event, "source": ref.short,
                                       "hint": "the PR mentions this issue without closing it: say briefly what it "
                                               "changes for it; no status change, never close"})
        elif c["link"] == "closes" and kind == "pr" and c["state"] == "OPEN":
            if event == "pr-opened":
                target = cfg["draft_pr_status"] if src["state"] == "draft" else cfg["pr_status"]
                plan["actions"].append({"op": "set-status", "issue": c["ref"], "status": target})
                plan["actions"].append({"op": "status-block", "issue": c["ref"], "pr": [ref.short],
                                        "status": target})
                plan["needs_text"].append({"op": "comment", "issue": c["ref"], "event": "pr-opened",
                                           "source": ref.short, "sha": (src["commits"][-1]["sha"][:12]
                                                                        if src["commits"] else None),
                                           "hint": "what this PR changes for the issue and what remains"})
            elif landed:
                plan["actions"].append({"op": "status-block", "issue": c["ref"], "pr": [ref.short]})
                plan["proposed_closes"].append({
                    "op": "close", "issue": c["ref"], "reason": "completed", "evidence": [src["url"]],
                    "source": ref.short, "open_items": c["checklist"]["open"],
                    "hint": "close only if the PR completes the issue (rubric); map every open item in criteria_map; "
                            "otherwise comment with event pr-merged and what remains"})
            else:
                plan["actions"].append({"op": "status-block", "issue": c["ref"], "pr": [ref.short]})
                plan["needs_text"].append({"op": "comment", "issue": c["ref"], "event": "pr-merged-partial",
                                           "source": ref.short, "sha": (src["merge_sha"] or "")[:12] or None,
                                           "hint": f"merged into {src['base']} (stacked), not yet in an integration "
                                                   "branch: say what landed, no closing"})
        else:
            plan["needs_judgment"].append(c)
    if kind == "pr" and not src["closing"] and not refs_in(source_text(src), ref.slug) \
            and not any(c["link"] for c in cands):
        plan["nudges"].append({"kind": "pr-links-no-issue",
                               "hint": f"the PR links no issue: add 'Refs owner/repo#N' (or 'Closes ...') to its body "
                                       f"with `gh pr edit {src['url']} --body-file <file>`, or link it in the "
                                       "Development sidebar"})
    return plan


def cmd_sync(ctx, args):
    plan = build_sync(ctx, args.source, args.event, args.repo)
    if args.apply:
        plan["applied"] = apply_actions(ctx, plan["actions"])
    emit(ctx, plan)
    return 0


def apply_actions(ctx, actions):
    results = []
    for action in actions:
        op = action.get("op")
        try:
            if op == "add-to-board":
                ref = parse_ref(action["issue"], ctx.slug)
                res = ensure_on_board(ctx, ref, status=action.get("status"), priority=action.get("priority"))
            elif op == "set-status":
                res = set_status(ctx, parse_ref(action["issue"], ctx.slug), action["status"],
                                 action.get("allow_backward", False))
            elif op == "set-priority":
                res = set_priority(ctx, parse_ref(action["issue"], ctx.slug, "issue"), action["priority"])
            elif op == "status-block":
                summary = action.get("summary")
                if isinstance(summary, str):
                    summary = [s for s in summary.splitlines() if s.strip()]
                covered = action.get("covered")
                if isinstance(covered, str):
                    repo, _, sha = covered.rpartition("=")
                    covered = {"repo": repo or ctx.slug, "sha": sha}
                res = status_block_upsert(ctx, parse_ref(action["issue"], ctx.slug, "issue"),
                                          status=action.get("status"), summary=summary, prs=action.get("pr") or [],
                                          covered=covered, criteria=action.get("criteria"))
            elif op == "comment":
                res = comment_upsert(ctx, parse_ref(action["issue"], ctx.slug, "issue"), action["event"],
                                     action["source"], action.get("body"), sha=action.get("sha"),
                                     key=action.get("key"))
            elif op == "close":
                res = close_issue(ctx, parse_ref(action["issue"], ctx.slug, "issue"), action.get("reason", "completed"),
                                  action.get("comment"), action.get("evidence") or [], action.get("criteria_map"),
                                  action.get("duplicate_of"), action.get("ack_activity", False), action.get("source"))
            else:
                raise Fail(f"unknown op {op!r}")
            results.append({"op": op, "ok": True, **res})
        except Refused as exc:
            results.append({"op": op, "ok": False, "refused": str(exc), **exc.data})
            if "in one run" in str(exc):
                break
        except Fail as exc:
            results.append({"op": op, "ok": False, "error": str(exc)})
            break
    return results


def cmd_apply(ctx, args):
    raw = sys.stdin.read() if args.plan == "-" else Path(args.plan).read_text(encoding="utf-8")
    plan = json.loads(raw)
    actions = plan.get("actions") if isinstance(plan, dict) else plan
    if not isinstance(actions, list):
        raise Fail("the plan must be a list of actions or an object with an `actions` list")
    results = apply_actions(ctx, actions)
    emit(ctx, {"results": results, "ok": all(r["ok"] for r in results)})
    return 0 if all(r["ok"] for r in results) else 3


# ---------------------------------------------------------------------------------------------- audit

def cmd_audit_scan(ctx, args):
    cfg = ctx.cfg()
    repo = ctx.repo
    if not repo.root or not repo.slug:
        raise Fail("audit-scan runs inside a checkout with a GitHub origin")
    slug = repo.slug
    branches = args.branch or integration_branches(ctx, slug)
    if not args.no_fetch:
        repo.git("fetch", "--quiet", "origin", *branches, check=False)
    heads = {}
    for b in branches:
        for r in (f"origin/{b}", b):
            if repo.ok("rev-parse", "--verify", "--quiet", r):
                heads[b] = {"ref": r, "sha": repo.git("rev-parse", r)}
                break
    if not heads:
        raise Fail(f"none of the integration branches ({', '.join(branches)}) exist locally; fetch them first")

    mention = re.compile(rf"(?:github\.com/{re.escape(slug)}\b|\b{re.escape(slug)}#\d+)", re.I)
    issues = [i for i in ctx.project().items() if i.get("type") == "Issue" and i["state"] == "OPEN" and
              (i["repository"]["nameWithOwner"].lower() == slug.lower() or mention.search(i.get("body") or ""))]
    if args.limit:
        issues = issues[:args.limit]
    since_floor = parse_time(args.since) if args.since and not re.fullmatch(r"[0-9a-f]{7,40}", args.since) else None

    # Merged PRs, newest first, back to the oldest watermark.
    watermarks = {}
    for it in issues:
        ref = parse_ref(it["url"])
        state = block_state(it.get("body") or "")
        cov = (state.get("covered") or {}).get(slug)
        _, marks = last_markers(ctx, ref)
        own_source = re.compile(rf"^{re.escape(slug)}[#@]", re.I)
        ours = [m for m in marks if m.get("sha") and own_source.match(m.get("source") or "")]
        if cov:
            wm = {"sha": cov.get("sha"), "date": cov.get("date") or it.get("createdAt"), "source": "status block"}
        elif ours:
            wm = {"sha": ours[-1]["sha"], "date": ours[-1]["date"], "source": f"marker {ours[-1].get('event')}"}
        else:
            wm = {"sha": None, "date": it.get("createdAt"), "source": "issue created"}
        if since_floor and parse_time(wm["date"]) < since_floor:
            wm = {"sha": None, "date": iso(since_floor), "source": "--since"}
        if args.since and re.fullmatch(r"[0-9a-f]{7,40}", args.since):
            wm = {"sha": args.since, "date": wm["date"], "source": "--since"}
        watermarks[it["url"]] = (wm, marks)
    oldest = min((parse_time(w["date"]) for w, _ in watermarks.values()), default=now() - dt.timedelta(days=30))
    merged, after = [], None
    while True:
        data = ctx.gh.graphql(Q_MERGED, owner=slug.split("/")[0], repo=slug.split("/")[1], after=after)
        page = data["repository"]["pullRequests"]
        stop = False
        for pr in page["nodes"]:
            # Pages are ordered by updatedAt, so only that field can end the scan.
            if pr.get("mergedAt") and parse_time(pr["mergedAt"]) >= oldest:
                merged.append(pr)
            if pr.get("updatedAt") and parse_time(pr["updatedAt"]) < oldest:
                stop = True
        if stop or not page["pageInfo"]["hasNextPage"] or len(merged) > 300:
            break
        after = page["pageInfo"]["endCursor"]
    for pr in merged:
        pr["merged"] = True
        pr["repository"] = {"nameWithOwner": slug}
        sha = (pr.get("mergeCommit") or {}).get("oid")
        pr["_landed"] = [b for b, h in heads.items() if pr["baseRefName"] == b or
                         (sha and repo.ok("merge-base", "--is-ancestor", sha, h["ref"]))]
        # A squash merge's message usually carries the PR's Closes/Refs lines: count it as part of the PR.
        pr["_merge_message"] = repo.git("log", "-1", "--format=%B", sha, check=False) if sha else ""

    commit_cache = {}

    def commits_since(wm):
        key = (wm.get("sha"), wm.get("date"))
        if key in commit_cache:
            return commit_cache[key]
        found = {}
        for b, h in heads.items():
            if wm.get("sha") and repo.ok("merge-base", "--is-ancestor", wm["sha"], h["ref"]):
                rng = [f"{wm['sha']}..{h['ref']}"]
            else:
                rng = [f"--since={wm['date']}", h["ref"]]
            sep, rec = "\x1f", "\x1e"
            log = repo.git("log", "--no-merges", f"--format={rec}%H{sep}%s{sep}%b{sep}", "--name-only", *rng,
                           check=False)
            for chunk in log.split(rec):
                if not chunk.strip():
                    continue
                sha, subject, body, rest = (chunk.split(sep) + ["", "", "", ""])[:4]
                entry = found.setdefault(sha.strip(), {"sha": sha.strip(), "subject": subject, "body": body,
                                                       "files": [l.strip() for l in rest.splitlines() if l.strip()],
                                                       "landed_in": []})
                entry["landed_in"].append(b)
        commit_cache[key] = list(found.values())
        return commit_cache[key]

    out_issues, synced_prs = [], set()
    for it in issues:
        ref = parse_ref(it["url"])
        wm, marks = watermarks[it["url"]]
        body = it.get("body") or ""
        iid = title_id(cfg, it["title"])
        ipaths = paths_in(body)
        state = block_state(body)
        for m in marks:
            if m.get("source"):
                synced_prs.add(m["source"])
        for p in state.get("prs") or []:
            synced_prs.add(p["ref"])
        evidence = []
        for pr in merged:
            if parse_time(pr["mergedAt"]) < parse_time(wm["date"]):
                continue
            reasons = []
            if any(ref.same(parse_ref(n["url"])) for n in pr["closingIssuesReferences"]["nodes"]):
                reasons.append("linked")
            text = "\n".join([pr["title"], pr.get("body") or "", pr["headRefName"], pr.get("_merge_message", "")] +
                             [n["commit"]["messageHeadline"] + "\n" + (n["commit"].get("messageBody") or "")
                              for n in pr["commits"]["nodes"]])
            if any(ref.same(r) for r in closing_refs_in(text, slug)):
                reasons.append("closing-keyword")
            elif any(ref.same(r) for r in refs_in(text, slug)):
                reasons.append("explicit-ref")
            if iid and iid in task_ids(cfg, pr["title"] + "\n" + pr["headRefName"].upper()):
                reasons.append(f"task-id {iid}")
            hits = path_overlap(ipaths, [n["path"] for n in pr["files"]["nodes"]])
            if hits:
                reasons.append("paths " + ", ".join(hits[:3]))
            head = pr["headRefName"]
            if pr["url"] in body or (len(head) > 4 and head not in heads and head in body):
                reasons.append("mentioned-by-issue")
            if reasons:
                evidence.append({"kind": "pr", "ref": f"{slug}#{pr['number']}", "title": pr["title"],
                                 "merged_at": pr["mergedAt"], "merged_into": pr["baseRefName"],
                                 "landed_in": pr["_landed"], "reasons": reasons,
                                 "closes": any(r in ("linked", "closing-keyword") or r.startswith("task-id")
                                               for r in reasons),
                                 "explicit": any(r in ("linked", "closing-keyword", "explicit-ref") or
                                                 r.startswith("task-id") for r in reasons)})
        pr_shas = set()
        for pr in merged:
            pr_shas |= {n["commit"]["oid"] for n in pr["commits"]["nodes"]}
            if (pr.get("mergeCommit") or {}).get("oid"):
                pr_shas.add(pr["mergeCommit"]["oid"])
        for c in commits_since(wm):
            if c["sha"] in pr_shas:
                continue
            reasons = []
            message = c["subject"] + "\n" + c["body"]
            if any(ref.same(r) for r in closing_refs_in(message, slug)):
                reasons.append("closing-keyword")
            elif any(ref.same(r) for r in refs_in(message, slug)):
                reasons.append("explicit-ref")
            if iid and iid in task_ids(cfg, c["subject"]):
                reasons.append(f"task-id {iid}")
            hits = path_overlap(ipaths, c["files"])
            if hits:
                reasons.append("paths " + ", ".join(hits[:3]))
            if reasons:
                evidence.append({"kind": "commit", "ref": c["sha"][:12], "title": c["subject"],
                                 "landed_in": c["landed_in"], "reasons": reasons,
                                 "closes": any(r == "closing-keyword" or r.startswith("task-id") for r in reasons),
                                 "explicit": any(r in ("closing-keyword", "explicit-ref") or r.startswith("task-id")
                                                 for r in reasons)})
        cl = checklist(body)
        suggested = "none"
        if any(e["closes"] and e["landed_in"] for e in evidence):
            suggested = "review-close"
        elif evidence:
            suggested = "comment"
        out_issues.append({"url": it["url"], "ref": ref.short, "title": it["title"], "status": it.get("status"),
                           "priority": it.get("priority"), "watermark": wm, "evidence": evidence,
                           "checklist": {"open": cl["open"], "done_count": len(cl["done"])},
                           "status_block": state.get("summary"), "suggested": suggested})
    unsynced = []
    for pr in merged:
        short = f"{slug}#{pr['number']}"
        if short in synced_prs or not pr["_landed"]:
            continue
        if any(short == e["ref"] for i in out_issues for e in i["evidence"] if e["explicit"]):
            continue
        unsynced.append({"ref": short, "url": pr["url"], "title": pr["title"], "merged_at": pr["mergedAt"],
                         "merged_into": pr["baseRefName"], "landed_in": pr["_landed"],
                         "linked": [parse_ref(n["url"]).short for n in pr["closingIssuesReferences"]["nodes"]]})
    emit(ctx, {"repo": slug, "project": ctx.project().label, "heads": {b: h["sha"] for b, h in heads.items()},
               "issues": out_issues, "unsynced_merged_prs": unsynced,
               "covered_hint": f"after handling an issue, record the check with status-block --covered "
                               f"{slug}={next(iter(heads.values()))['sha']}"})
    return 0


# ---------------------------------------------------------------------------------------------- estimates

def session_hours(stamps, gap_minutes, lead_minutes):
    """Hands-on time from commit timestamps: commits closer than the gap form one session, and each session counts
    its span plus a lead-in for the work before its first commit."""
    stamps = sorted(set(stamps))
    if not stamps:
        return None
    total, start, prev = 0.0, stamps[0], stamps[0]
    for t in stamps[1:]:
        if (t - prev).total_seconds() > gap_minutes * 60:
            total += (prev - start).total_seconds() + lead_minutes * 60
            start = t
        prev = t
    total += (prev - start).total_seconds() + lead_minutes * 60
    return round(total / 3600, 2)


def git_actual(ctx, item):
    """Actual hours of a closed issue from the commits of its merged closing PRs (None without any)."""
    est = ctx.cfg()["estimate"]
    stamps = []
    for pr in (item.get("closedByPullRequestsReferences") or {}).get("nodes", []):
        if pr.get("state") != "MERGED":
            continue
        ref = parse_ref(pr["url"])
        data = ctx.gh.graphql(Q_PR_COMMITS, owner=ref.owner, repo=ref.repo, number=ref.number)
        node = ((data.get("repository") or {}).get("pullRequest") or {})
        stamps += [parse_time(n["commit"]["authoredDate"]) for n in (node.get("commits") or {}).get("nodes", [])
                   if (n.get("commit") or {}).get("authoredDate")]
    return session_hours(stamps, est["session_gap_minutes"], est["session_lead_minutes"])


def done_item(item):
    return item.get("type") == "Issue" and item.get("state") == "CLOSED" and \
        (item.get("stateReason") or "COMPLETED") == "COMPLETED"


def history_entry(ctx, item, need_estimate=True):
    """Estimate and actual of a closed issue, or None when one of them is missing."""
    state = block_state(item.get("body") or "")
    est = state.get("estimate") or {}
    # Calibrate against the raw (uncalibrated) number, so the factor doesn't feed on itself.
    estimate = est.get("raw") or est.get("hours") or item.get("estimate")
    if need_estimate and not estimate:
        return None
    if (state.get("actual") or {}).get("hours"):
        actual, source = float(state["actual"]["hours"]), state["actual"].get("source", "reported")
    else:
        actual, source = git_actual(ctx, item), "git"
    if not actual:
        return None
    ref = parse_ref(item["url"])
    return {"issue": ref.short, "title": item["title"], "estimate": float(estimate) if estimate else None,
            "board_estimate": item.get("estimate"), "actual": actual, "actual_source": source,
            "closed": item.get("closedAt")}


def calibration(ctx):
    est = ctx.cfg()["estimate"]
    samples = [h for h in (history_entry(ctx, i) for i in ctx.project().items() if done_item(i)) if h]
    reported = [s for s in samples if s["actual_source"] == "reported"]
    used = reported if len(reported) >= est["min_samples"] else samples
    out = {"samples": samples, "used": len(used), "reported": len(reported), "min_samples": est["min_samples"]}
    if len(used) < est["min_samples"]:
        out.update(factor=float(est["prior_factor"]), basis="prior",
                   note=f"{len(used)} closed issue(s) with an estimate and an actual; the prior factor applies until "
                        f"there are {est['min_samples']}")
        return out
    ratios = sorted(s["actual"] / s["estimate"] for s in used)
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    # A handful of odd issues must not swing every future estimate by more than 4x either way.
    out.update(factor=round(min(4.0, max(0.25, median)), 2),
               basis="reported actuals" if used is reported else "reported and git-proxy actuals")
    return out


def snap_hours(cfg, hours):
    est = cfg["estimate"]
    if hours > est["max_hours"]:
        return float(math.ceil(hours))
    return float(min(est["scale"], key=lambda v: abs(math.log(v / hours))))


def board_item(ctx, ref):
    for item in ctx.project().items():
        if item.get("type") == "Issue" and parse_ref(item["url"]).same(ref):
            return item
    return None


def estimate_view(ctx, ref, item):
    issue = item or load_issue(ctx, ref)
    body = issue.get("body") or ""
    items = checklist(body)
    anchors = []
    src = {**source_from_text(issue["title"], body), "ref": ref}
    for cand in candidates(ctx, src, limit=25, min_score=0.05, include_closed=True):
        if cand["state"] != "CLOSED" or len(anchors) >= 5:
            continue
        closed = board_item(ctx, parse_ref(cand["url"]))
        entry = history_entry(ctx, closed, need_estimate=False) if closed and done_item(closed) else None
        if entry:
            anchors.append({**entry, "similarity": cand["score"]})
    return {"issue": ref.short, "url": issue["url"], "title": issue["title"], "state": issue["state"],
            "status": (item or {}).get("status"), "estimate": (item or {}).get("estimate"),
            "block_estimate": block_state(body).get("estimate"),
            "open_items": [{"n": n, **i} for n, i in enumerate(items["open"], 1)], "done_items": len(items["done"]),
            "anchors": anchors}


def check_estimate(ctx, entry, cal, overwrite):
    """Validate one plan entry and compute its hours. Returns (result, problems)."""
    cfg = ctx.cfg()
    ref = parse_ref(str(entry.get("issue", "")), ctx.slug, "issue")
    if not ref:
        return None, [f"bad issue {entry.get('issue')!r}"]
    problems = []
    item = board_item(ctx, ref)
    issue = item or load_issue(ctx, ref)
    open_items = checklist(issue.get("body") or "")["open"]
    lanes = entry.get("lanes") or []
    if not lanes:
        problems.append("no lanes: group the open checklist items into lanes that can run in parallel")
    seen = {}
    for lane in lanes:
        if not isinstance(lane.get("hours"), (int, float)) or lane["hours"] <= 0:
            problems.append(f"lane {lane.get('name')!r}: hours must be a number above 0")
        for key in lane.get("items") or []:
            idx = None
            if isinstance(key, int) or str(key).isdigit():
                idx = int(key) if 1 <= int(key) <= len(open_items) else None
            else:
                idx = next((n for n, i in enumerate(open_items, 1) if i["text"] == str(key).strip()), None)
            if open_items and idx is None:
                problems.append(f"lane {lane.get('name')!r}: {key!r} is not an open checklist item")
            elif idx is not None:
                if idx in seen:
                    problems.append(f"item {idx} is in both lane {seen[idx]!r} and lane {lane.get('name')!r}")
                seen[idx] = lane.get("name")
    missing = [n for n in range(1, len(open_items) + 1) if n not in seen]
    if missing:
        problems.append("open checklist items in no lane: " +
                        "; ".join(f"{n}. {open_items[n - 1]['text']}" for n in missing))
    extra = {}
    for key in ("brief_hours", "integration_hours"):
        value = entry.get(key, 0)
        if not isinstance(value, (int, float)) or value < 0:
            problems.append(f"{key} must be a number, 0 or more")
        extra[key] = value if isinstance(value, (int, float)) else 0
    if problems:
        return {"issue": ref.short}, problems
    raw = round(extra["brief_hours"] + sum(lane["hours"] for lane in lanes) + extra["integration_hours"], 2)
    hours = snap_hours(cfg, raw * cal["factor"])
    current = (item or {}).get("estimate")
    ours = (block_state(issue.get("body") or "").get("estimate") or {}).get("hours")
    if current is not None and float(current) != hours and not overwrite and \
            (ours is None or float(ours) != float(current)):
        problems.append(f"the board already says {fmt_hours(current)} h, set by someone else; ask the user, then "
                        "pass --overwrite")
    warnings = []
    if len(lanes) == 1 and len(open_items) >= 4:
        warnings.append("all items in one lane: check whether some can run in parallel")
    if raw * cal["factor"] > cfg["estimate"]["max_hours"]:
        warnings.append(f"over {cfg['estimate']['max_hours']} h: consider splitting it into sub-issues")
    result = {"issue": ref.short, "raw": raw, "factor": cal["factor"], "factor_basis": cal["basis"], "hours": hours,
              "previous": current, "warnings": warnings,
              "_ref": ref, "_item": item, "_issue": issue,
              "_state": {"hours": hours, "raw": raw, "factor": cal["factor"],
                         "brief": extra["brief_hours"], "integration": extra["integration_hours"],
                         "lanes": [{"name": str(lane.get("name") or f"lane {n}"), "items": len(lane.get("items") or []),
                                    "hours": lane["hours"]} for n, lane in enumerate(lanes, 1)],
                         "by": ctx.agent, "date": iso(now())}}
    return result, problems


def apply_estimates(ctx, plan, overwrite):
    cfg = ctx.cfg()
    ctx.project().number_field(cfg["estimate"]["field"])
    cal = calibration(ctx)
    checked = [check_estimate(ctx, entry, cal, overwrite) for entry in plan]
    problems = {res["issue"] if res else str(i): p for i, (res, p) in enumerate(checked) if p}
    if problems:
        raise Refused("the estimate plan has problems; fix them and run it again (nothing was written)",
                      {"problems": problems})
    out = []
    for res, _ in checked:
        ref, item, issue = res.pop("_ref"), res.pop("_item"), res.pop("_issue")
        state = res.pop("_state")
        if item:
            item_id = item["item_id"]
        else:
            item_id = ensure_on_board(ctx, ref, node_id=issue["id"],
                                      project_items=(issue.get("projectItems") or {}).get("nodes", []))["item_id"]
        if res["previous"] is None or float(res["previous"]) != res["hours"]:
            ctx.count()
            ctx.project().set_number(item_id, cfg["estimate"]["field"], res["hours"])
            res["board"] = "set"
        else:
            res["board"] = "noop"
        status = status_key(cfg, (item or {}).get("status"))
        res["status_block"] = status_block_upsert(ctx, ref, status=status, estimate=state)["action"]
        out.append(res)
    return {"estimates": out, "calibration": {k: v for k, v in cal.items() if k != "samples"}}


def cmd_estimate(ctx, args):
    cfg = ctx.cfg()
    if args.calibration:
        emit(ctx, calibration(ctx))
        return 0
    if args.apply:
        if not args.plan:
            raise Fail("--apply needs --plan <file> (a list of {issue, brief_hours, lanes, integration_hours})")
        raw = sys.stdin.read() if args.plan == "-" else Path(args.plan).read_text(encoding="utf-8")
        plan = json.loads(raw)
        plan = plan.get("estimates") if isinstance(plan, dict) else plan
        if not isinstance(plan, list):
            raise Fail("the plan must be a list of estimates or an object with an `estimates` list")
        emit(ctx, apply_estimates(ctx, plan, args.overwrite))
        return 0
    if args.actual is not None:
        if len(args.issue or []) != 1:
            raise Fail("--actual needs exactly one --issue")
        if args.actual <= 0:
            raise Fail("--actual must be above 0 hours")
        ref = parse_ref(args.issue[0], ctx.slug, "issue")
        actual = {"hours": float(args.actual), "source": "reported", "date": iso(now())}
        emit(ctx, {"issue": ref.short, "actual": actual,
                   "status_block": status_block_upsert(ctx, ref, actual=actual)["action"]})
        return 0
    targets = []
    if args.issue:
        for text in args.issue:
            ref = parse_ref(text, ctx.slug, "issue")
            if not ref:
                raise Fail(f"cannot resolve issue {text!r}")
            targets.append((ref, board_item(ctx, ref)))
    elif args.unestimated:
        slug = args.repo or ctx.slug
        for item in sorted(ctx.project().items(), key=lambda i: (i.get("repository") or {}).get("nameWithOwner", "") +
                           f"#{i.get('number', 0):08d}"):
            if item.get("type") != "Issue" or item.get("state") != "OPEN" or item.get("estimate") is not None:
                continue
            if slug and (item.get("repository") or {}).get("nameWithOwner", "").lower() != slug.lower():
                continue
            if args.status and status_key(cfg, item.get("status")) != args.status:
                continue
            targets.append((parse_ref(item["url"]), item))
            if len(targets) >= args.limit:
                break
    else:
        raise Fail("pass --issue <i> (repeatable), --unestimated, --calibration, or --apply --plan <file>")
    cal = calibration(ctx)
    emit(ctx, {"issues": [estimate_view(ctx, ref, item) for ref, item in targets],
               "calibration": {k: v for k, v in cal.items() if k != "samples"},
               "scale": cfg["estimate"]["scale"], "max_hours": cfg["estimate"]["max_hours"],
               "next": "for each issue, group the open items into parallel lanes and price human hands-on time per "
                       "lane (references/estimates.md); then run estimate --apply --plan <file> with "
                       "[{issue, brief_hours, lanes: [{name, items: [<n or text>], hours}], integration_hours}]"})
    return 0


# ---------------------------------------------------------------------------------------------- parser

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", metavar="DIR", default=argparse.SUPPRESS, help="run as if in DIR")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="show the writes without making them")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    common.add_argument("--agent", default=argparse.SUPPRESS, help="agent name for comment footers")

    parser = argparse.ArgumentParser(prog="gh-proman", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    parser.add_argument("--version", action="version", version=f"gh-proman {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        p = sub.add_parser(name, help=help_text, parents=[common])
        p.set_defaults(fn=fn)
        return p

    add("doctor", cmd_doctor, "check gh, auth scopes, config, project fields and branches")

    p = add("config", cmd_config, "show or write the gh-proman config")
    p.add_argument("action", choices=["show", "init"])
    p.add_argument("--owner")
    p.add_argument("--number", type=int)
    p.add_argument("--language", choices=list(I18N), default="it")
    p.add_argument("--branches", help="comma-separated integration branches (default: @default)")
    p.add_argument("--user", action="store_true", help=f"write a profile in {user_config_path()} instead")
    p.add_argument("--match", action="append", help="owner/repo globs for the user profile (default owner/*)")
    p.add_argument("--force", action="store_true")

    p = add("project", cmd_project_setup, "set up project fields (Priority options, Estimate)")
    p.add_argument("action", choices=["setup"])

    p = add("permalink", cmd_permalink, "SHA permalink for path[:line[-line]]")
    p.add_argument("path")
    p.add_argument("--ref", default="HEAD")

    p = add("lint-body", cmd_lint, "check an issue body before creating it")
    p.add_argument("--type", choices=["bug", "feature", "chore"])
    p.add_argument("--body-file")
    p.add_argument("--body")
    p.add_argument("--language", choices=list(I18N))

    p = add("candidates", cmd_candidates, "rank project issues related to a PR, issue, git range or draft")
    p.add_argument("--pr")
    p.add_argument("--issue")
    p.add_argument("--range")
    p.add_argument("--title")
    p.add_argument("--body-file")
    p.add_argument("--body")
    p.add_argument("--repo")
    p.add_argument("--mode", choices=["related", "duplicate"], default="related")
    p.add_argument("--limit", type=int, default=10)

    p = add("create", cmd_create, "create an issue on the board (with duplicate guard)")
    p.add_argument("--repo")
    p.add_argument("--type", choices=["bug", "feature", "chore"], required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body-file")
    p.add_argument("--body")
    p.add_argument("--priority")
    p.add_argument("--id", help="task ID for the title prefix, e.g. S03")
    p.add_argument("--status", choices=STATUS_ORDER, default="backlog")
    p.add_argument("--parent", help="parent issue (adds this one as a sub-issue)")
    p.add_argument("--confirm-new", action="store_true", help="skip the duplicate guard (after the user chose)")

    p = add("findings", cmd_findings, "draft or create issues for review findings a forge run left open")
    p.add_argument("--from", dest="source", required=True,
                   help="forge plan dir, task/run dir, known_issues.md or qa.last")
    p.add_argument("--include-plausible", action="store_true", help="also PLAUSIBLE findings, as 'to verify' issues")
    p.add_argument("--group", choices=["finding", "task", "run"], default="finding",
                   help="one issue per finding (default), per task, or one for the whole run")
    p.add_argument("--min-severity", choices=SEVERITY_ORDER, help="skip findings milder than this")
    p.add_argument("--repo", help="owner/repo when it can't be read from the plan")
    p.add_argument("--apply", action="store_true", help="create the issues (default: only show drafts)")
    p.add_argument("--confirm-new", action="store_true", help="create even when a similar issue exists")
    p.add_argument("--max-issues", type=int, default=10)
    p.add_argument("--show-bodies", action="store_true", help="print full draft bodies")

    p = add("comment", cmd_comment, "post or update a gh-proman comment (idempotent by event+source)")
    p.add_argument("--issue", required=True)
    p.add_argument("--event", required=True, choices=["issue-related", "duplicate-candidate", "pr-opened",
                                                       "pr-merged", "pr-merged-partial", "closed", "audit", "note"])
    p.add_argument("--source", required=True, help="what triggered it: owner/repo#N, owner/repo@branch, …")
    p.add_argument("--sha")
    p.add_argument("--key", help="extra idempotency key (e.g. the audited head)")
    p.add_argument("--body-file")
    p.add_argument("--body")

    p = add("status-block", cmd_status_block, "update the status block in the issue body")
    p.add_argument("--issue", required=True)
    p.add_argument("--status", choices=STATUS_ORDER)
    p.add_argument("--summary", action="append", help="summary line (repeatable)")
    p.add_argument("--summary-file")
    p.add_argument("--pr", action="append", help="PR to track (repeatable)")
    p.add_argument("--covered", help="owner/repo=<sha>: code checked up to this commit")
    p.add_argument("--criteria", help="e.g. 2/3")

    p = add("set-status", cmd_set_status, "move the board Status (forward only by default)")
    p.add_argument("--issue", required=True)
    p.add_argument("--status", required=True, choices=STATUS_ORDER)
    p.add_argument("--allow-backward", action="store_true")

    p = add("set-priority", cmd_set_priority, "set Priority (and the [ID · Pn] title prefix)")
    p.add_argument("--issue", required=True)
    p.add_argument("--priority", required=True)

    p = add("close", cmd_close, "close an issue after the auto-close guards pass")
    p.add_argument("--issue", required=True)
    p.add_argument("--reason", choices=["completed", "not_planned", "duplicate"], default="completed")
    p.add_argument("--duplicate-of")
    p.add_argument("--evidence", action="append", help="merged PR or commit URL (repeatable)")
    p.add_argument("--comment-file")
    p.add_argument("--comment")
    p.add_argument("--criteria-map", help="JSON file {\"<open item text or 1-based index>\": \"<evidence>\"}")
    p.add_argument("--source")
    p.add_argument("--ack-activity", action="store_true", help="recent comments by others were read")

    p = add("sync", cmd_sync, "plan the sync for a new issue/PR or a merge")
    p.add_argument("--source", required=True, help="issue/PR URL, owner/repo#N, #N or a branch")
    p.add_argument("--event", choices=["issue-created", "pr-opened", "pr-merged"])
    p.add_argument("--repo")
    p.add_argument("--apply", action="store_true", help="also run the deterministic actions")

    p = add("apply", cmd_apply, "run a plan of actions (JSON file or -)")
    p.add_argument("--plan", required=True)

    p = add("estimate", cmd_estimate, "estimate hands-on hours for issues, record actuals, show calibration")
    p.add_argument("--issue", action="append", help="issue to estimate (repeatable)")
    p.add_argument("--unestimated", action="store_true", help="open board issues of this repo with no estimate")
    p.add_argument("--status", choices=STATUS_ORDER, help="with --unestimated: only issues in this status")
    p.add_argument("--repo", help="with --unestimated: owner/repo (default: this repo)")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--apply", action="store_true", help="write the estimates in --plan")
    p.add_argument("--plan", help="JSON file or - : [{issue, brief_hours, lanes, integration_hours}]")
    p.add_argument("--overwrite", action="store_true", help="replace an Estimate someone else set")
    p.add_argument("--actual", type=float, help="record the hands-on hours an issue really took")
    p.add_argument("--calibration", action="store_true", help="list the closed issues the factor is computed from")

    p = add("audit-scan", cmd_audit_scan, "collect evidence for open board issues of this repo")
    p.add_argument("--branch", action="append")
    p.add_argument("--since", help="SHA or date: ignore older changes")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--limit", type=int)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "C", None):
        os.chdir(args.C)
        args.C = "."
    ctx = Ctx(args)
    try:
        return args.fn(ctx, args) or 0
    except Fail as exc:
        payload = {"error" if exc.code == 2 else "refused": str(exc), **exc.data}
        if ctx.json or exc.code == 3:
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"gh-proman: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
