"""Black-box tests for gh_proman.py and hook.py, run against a stateful fake `gh` and throwaway git repos.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "gh-proman" / "scripts"
CLI = SCRIPTS / "gh_proman.py"
HOOK = SCRIPTS / "hook.py"
FAKES = Path(__file__).resolve().parent / "fakes"

COLLEAGUE_BODY = (
    "Piano unico: https://github.com/o/r/issues/2\r\n\r\n"
    "| Campo | Valore |\r\n| --- | --- |\r\n| Priorità | P0 |\r\n\r\n"
    "## Criteri di chiusura e prove\r\n\r\n"
    "- [ ] Proroga valida prevale; «da definire» non maschera un termine scaduto.\r\n"
    "- [x] Tabella dei casi fissata\r\n"
    "Riferimento: `src/lib/matching-availability.ts:18`\r\n"
    "<!-- implementation-readiness-2026-10-20:start -->\n## Dettagli — verifica del 20/10/2026\n"
    "Testo dell’analisi.\n<!-- implementation-readiness-2026-10-20:end -->\n"
    "### Avanzamento libero senza marker\n"
)


def base_state():
    status = {"id": "F_S", "name": "Status", "dataType": "SINGLE_SELECT",
              "options": [{"id": f"s{i}", "name": n} for i, n in
                          enumerate(["Backlog", "Ready", "In progress", "In review", "Done"])]}
    priority = {"id": "F_P", "name": "Priority", "dataType": "SINGLE_SELECT", "options": []}
    return {
        "viewer": "me", "seq": 1000, "clock": 0, "now": "2026-10-21T10:00:00Z",
        "repos": {
            "o/r": {"default_branch": "main", "branches": ["main", "dev"], "issues": {}, "pulls": {}, "compare": {},
                    "commits": {}},
            "o/other": {"default_branch": "main", "branches": ["main"], "issues": {}, "pulls": {}, "compare": {}},
        },
        "projects": {"o/1": {"id": "PVT_1", "title": "Board", "fields": [status, priority], "items": []}},
    }


class Case(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "gh.log"
        self.root = self.dir / "repo"
        self.root.mkdir()
        self.state = base_state()
        self.save()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "GH_", "CODEX_")) and k not in ("CLAUDECODE", "GITHUB_TOKEN")}
        self.env.update({
            "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}", "GH_FAKE_STATE": str(self.state_path),
            "GH_FAKE_LOG": str(self.log_path), "GH_PROMAN_CONFIG": str(self.dir / "user-config.json"),
            "GH_PROMAN_NOW": "2026-10-21T10:00:00Z", "GH_PROMAN_AGENT": "Test", "HOME": str(self.dir / "home"),
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        })
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "https://github.com/o/r.git")
        self.write_config()

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------------------------------------

    def save(self):
        self.state_path.write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads(self.state_path.read_text())
        return self.state

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, message, path=None):
        target = self.root / (path or f"f{len(list(self.root.glob('**/*')))}.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def write_config(self, **extra):
        cfg = {"version": 1, "language": "it", "project": {"owner": "o", "number": 1},
               "integration_branches": ["dev", "@default"], **extra}
        (self.root / ".github").mkdir(exist_ok=True)
        (self.root / ".github" / "gh-proman.json").write_text(json.dumps(cfg))

    def add_issue(self, number, title, body="", author="me", created="2026-10-01T09:00:00Z", board=True,
                  status="Backlog", slug="o/r", comments=None, state="OPEN"):
        issue = {"number": number, "node_id": f"I_{slug}_{number}", "title": title, "body": body, "state": state,
                 "author": author, "created_at": created, "updated_at": created, "comments": comments or []}
        self.state["repos"][slug]["issues"][str(number)] = issue
        if board:
            self.state["projects"]["o/1"]["items"].append(
                {"id": f"PVTI_{slug}_{number}", "content": f"{slug}#{number}",
                 "fields": {"Status": status} if status else {}})
        self.save()
        return issue

    def add_pr(self, number, title, body="", head="feat/x", base="dev", state="OPEN", draft=False, closing=(),
               files=(), commits=(), merge_sha=None, merged_at=None, slug="o/r"):
        pr = {"number": number, "node_id": f"PR_{slug}_{number}", "title": title, "body": body, "head": head,
              "base": base, "state": state, "draft": draft, "closing": list(closing), "files": list(files),
              "commits": [{"oid": f"{i:040x}", "headline": c} for i, c in enumerate(commits, 1)],
              "merge_sha": merge_sha, "merged_at": merged_at}
        self.state["repos"][slug]["pulls"][str(number)] = pr
        self.save()
        return pr

    def cli(self, *args, expect=0, stdin=None):
        proc = subprocess.run([sys.executable, str(CLI), *args], cwd=self.root, env=self.env, capture_output=True,
                              text=True, input=stdin)
        self.assertEqual(proc.returncode, expect, f"gh-proman {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def cj(self, *args, expect=0, stdin=None):
        return json.loads(self.cli(*args, "--json", expect=expect, stdin=stdin))

    def hook(self, payload, env=None):
        proc = subprocess.run([sys.executable, str(HOOK)], cwd=self.root, env={**self.env, **(env or {})},
                              input=payload if isinstance(payload, str) else json.dumps(payload),
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def calls(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def mutations(self):
        out = []
        for c in self.calls():
            argv = c["argv"]
            if argv[:2] == ["api", "graphql"] and c["input"]["query"].lstrip().startswith("mutation"):
                out.append(c["input"]["query"].split()[1].split("(")[0])
            elif argv[:1] == ["api"] and "-X" in argv:
                out.append(argv[argv.index("-X") + 1] + " " + argv[1])
        return out

    def issue(self, number, slug="o/r"):
        return self.load()["repos"][slug]["issues"][str(number)]

    def item(self, content):
        return next(i for i in self.load()["projects"]["o/1"]["items"] if i["content"] == content)


# ---------------------------------------------------------------------------------------------- config

class ConfigTests(Case):
    def test_repo_config_overrides_user_profile(self):
        Path(self.env["GH_PROMAN_CONFIG"]).write_text(json.dumps(
            {"profiles": [{"match": ["o/*"], "language": "en", "project": {"owner": "o", "number": 9}}]}))
        data = self.cj("config", "show")
        self.assertEqual(data["config"]["language"], "it")
        self.assertEqual(data["config"]["project"]["number"], 1)
        self.assertEqual(len(data["sources"]), 2)

    def test_user_profile_applies_by_glob(self):
        (self.root / ".github" / "gh-proman.json").unlink()
        Path(self.env["GH_PROMAN_CONFIG"]).write_text(json.dumps(
            {"profiles": [{"match": ["x/*"], "project": {"owner": "x", "number": 2}},
                          {"match": ["O/*"], "language": "en", "project": {"owner": "o", "number": 1}}]}))
        data = self.cj("config", "show")
        self.assertEqual(data["config"]["language"], "en")
        self.assertIn("profile O/*", data["sources"][0])

    def test_unconfigured_repo_errors_and_hook_stays_silent(self):
        (self.root / ".github" / "gh-proman.json").unlink()
        out = self.cj("config", "show", expect=2)
        self.assertIn("not configured", out["error"])
        self.add_issue(1, "Something")
        ctx = self.hook({"tool_name": "Bash", "cwd": str(self.root),
                         "tool_input": {"command": "gh issue create -t x -b y"},
                         "tool_response": {"stdout": "https://github.com/o/r/issues/1\n"}})
        self.assertIsNone(ctx)

    def test_config_init_writes_repo_file_and_refuses_overwrite(self):
        (self.root / ".github" / "gh-proman.json").unlink()
        self.cj("config", "init", "--owner", "o", "--number", "1", "--branches", "dev,@default")
        written = json.loads((self.root / ".github" / "gh-proman.json").read_text())
        self.assertEqual(written["integration_branches"], ["dev", "@default"])
        self.cj("config", "init", "--owner", "o", "--number", "1", expect=3)


# ---------------------------------------------------------------------------------------------- comments

class CommentTests(Case):
    def test_upsert_is_idempotent_and_updates_in_place(self):
        self.add_issue(1, "Bug")
        args = ["comment", "--issue", "o/r#1", "--event", "pr-opened", "--source", "o/r#5"]
        self.assertEqual(self.cj(*args, "--body", "First")["action"], "created")
        self.assertEqual(self.cj(*args, "--body", "First")["action"], "noop")
        self.assertEqual(self.cj(*args, "--body", "Second")["action"], "updated")
        comments = self.issue(1)["comments"]
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["body"].startswith("<!-- gh-proman:v1 event=pr-opened source=o/r#5 -->"))
        self.assertIn("Second", comments[0]["body"])
        self.assertIn("gh-proman · aggiornamento automatico (Test) · 21/10/2026", comments[0]["body"])
        self.assertEqual(self.cj(*args[:-1], "o/r#6", "--body", "Other")["action"], "created")

    def test_ignores_other_authors_markers_and_escapes_forged_markers(self):
        self.add_issue(1, "Bug", comments=[{"id": 1, "user": {"login": "colleague"}, "created_at": "2026-10-01T00:00:00Z",
                                            "body": "<!-- gh-proman:v1 event=note source=o/r#5 -->\nTheirs"}])
        res = self.cj("comment", "--issue", "o/r#1", "--event", "note", "--source", "o/r#5",
                      "--body", "Mine <!-- gh-proman:status:start -->")
        self.assertEqual(res["action"], "created")
        mine = self.issue(1)["comments"][1]["body"]
        self.assertIn("&lt;!-- gh-proman:status:start", mine)
        self.assertEqual(self.issue(1)["comments"][0]["body"].count("Theirs"), 1)


# ---------------------------------------------------------------------------------------------- status block

class StatusBlockTests(Case):
    def test_insert_preserves_every_foreign_byte_and_updates_in_place(self):
        self.add_issue(10, "[S03 · P0] Scadenza", COLLEAGUE_BODY)
        res = self.cj("status-block", "--issue", "o/r#10", "--status", "in_review", "--summary", "PR aperta")
        self.assertEqual(res["action"], "updated")
        body = self.issue(10)["body"]
        self.assertTrue(body.startswith("<!-- gh-proman:status:start -->"))
        self.assertTrue(body.endswith(COLLEAGUE_BODY))
        self.assertIn("> **Stato (gh-proman)** · In review · aggiornato 21/10/2026", body)
        self.cj("status-block", "--issue", "o/r#10", "--criteria", "1/2")
        body2 = self.issue(10)["body"]
        self.assertEqual(body2.count("gh-proman:status:start"), 1)
        self.assertTrue(body2.endswith(COLLEAGUE_BODY))
        self.assertIn("PR aperta", body2)  # summary kept from the first update
        self.assertIn("Criteri coperti: 1/2", body2)

    def test_noop_when_nothing_changes(self):
        self.add_issue(1, "x", "body")
        self.cj("status-block", "--issue", "o/r#1", "--status", "backlog")
        self.assertEqual(self.cj("status-block", "--issue", "o/r#1", "--status", "backlog")["action"], "noop")

    def test_tracks_prs_with_state(self):
        self.add_issue(1, "x", "body")
        self.add_pr(5, "PR", draft=True, base="dev")
        self.cj("status-block", "--issue", "o/r#1", "--pr", "o/r#5")
        self.assertIn("PR: o/r#5 (draft → dev)", self.issue(1)["body"])

    def test_refuses_unbalanced_markers(self):
        self.add_issue(1, "x", "<!-- gh-proman:status:start -->\nhalf")
        out = self.cj("status-block", "--issue", "o/r#1", "--status", "done", expect=3)
        self.assertIn("unbalanced", out["refused"])
        self.assertEqual(self.mutations(), [])

    def test_refuses_body_over_limit(self):
        self.add_issue(1, "x", "a" * 65500)
        self.cj("status-block", "--issue", "o/r#1", "--summary", "y" * 200, expect=3)

    def test_concurrent_edit_is_recomputed_not_lost(self):
        self.add_issue(1, "x", "original")
        self.state["race"] = {"issue": "o/r#1", "append": "\nadded by colleague", "on_get": 2}
        self.save()
        self.cj("status-block", "--issue", "o/r#1", "--status", "ready")
        body = self.issue(1)["body"]
        self.assertTrue(body.endswith("original\nadded by colleague"))
        self.assertEqual(body.count("gh-proman:status:start"), 1)

    def test_dry_run_makes_no_mutations(self):
        self.add_issue(1, "x", "body")
        out = self.cj("status-block", "--issue", "o/r#1", "--status", "ready", "--dry-run")
        self.assertEqual(out["action"], "would-update")
        self.assertEqual(self.mutations(), [])


# ---------------------------------------------------------------------------------------------- hook

class HookTests(Case):
    def setUp(self):
        super().setUp()
        self.add_issue(3, "Login fails")
        self.add_pr(7, "Fix login", body="Closes #3")

    def bash(self, command, response, **extra):
        return {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"command": command},
                "tool_response": response, **extra}

    def test_claude_issue_create(self):
        ctx = self.hook(self.bash("gh issue create --title x --body y",
                                  {"stdout": "Creating issue\nhttps://github.com/o/r/issues/3\n", "stderr": ""}))
        self.assertIn("you just opened issue o/r#3", ctx)
        self.assertIn("sync --source https://github.com/o/r/issues/3 --apply --json", ctx)
        self.assertIn("Write comments in Italian.", ctx)
        self.assertLess(len(ctx), 1200)

    def test_cd_and_repo_flag(self):
        sub = self.root / "sub"
        sub.mkdir()
        ctx = self.hook(self.bash(f"cd {sub} && gh -R o/r pr create --fill",
                                  {"stdout": "https://github.com/o/r/pull/7"}))
        self.assertIn("you just opened PR o/r#7", ctx)
        self.assertIn("Don't close issues when a PR is opened", ctx)
        self.assertNotIn("links no issue", ctx)  # --fill: the body is unknown, sync checks it

    def test_codex_list_command_and_output_shape(self):
        payload = {"tool_name": "Bash", "cwd": str(self.root),
                   "tool_input": {"command": ["bash", "-lc", "gh pr create -t T -b 'no refs here'"]},
                   "tool_response": {"output": "https://github.com/o/r/pull/7\n", "exit_code": 0}}
        ctx = self.hook(payload)
        self.assertIn("The PR body links no issue", ctx)

    def test_body_with_ref_has_no_nudge(self):
        ctx = self.hook(self.bash("gh pr create -t T --body 'Refs o/r#3'", "https://github.com/o/r/pull/7"))
        self.assertNotIn("links no issue", ctx)

    def test_body_file_is_read(self):
        (self.root / "body.md").write_text("Closes #3\n")
        ctx = self.hook(self.bash("gh pr create -t T --body-file body.md", "https://github.com/o/r/pull/7"))
        self.assertNotIn("links no issue", ctx)

    def test_merge_uses_selector(self):
        ctx = self.hook(self.bash("gh pr merge 7 --squash", {"stdout": ""}))
        self.assertIn("you just merged PR https://github.com/o/r/pull/7", ctx)
        self.assertIn("--event pr-merged", ctx)
        self.assertIn("rubric", ctx)

    def test_merge_current_branch_and_auto(self):
        ctx = self.hook(self.bash("gh pr merge --squash", {"stdout": ""}))
        self.assertIn("number is unknown", ctx)  # no commits yet: no branch to name
        self.commit("init")
        self.git("checkout", "-q", "-b", "feat/login")
        ctx = self.hook(self.bash("gh pr merge --squash --delete-branch", {"stdout": ""}, tool_use_id="t-branch"))
        self.assertIn("sync --source feat/login --repo o/r", ctx)
        ctx = self.hook(self.bash("gh pr merge --squash", {"stdout": "✓ Squashed and merged pull request o/r#7 (x)"},
                                  tool_use_id="t-out"))
        self.assertIn("https://github.com/o/r/pull/7", ctx)
        ctx = self.hook(self.bash("gh pr merge 8 --auto --squash", {"stdout": ""}, tool_use_id="t-auto"))
        self.assertIn("auto-merge was enabled", ctx)

    def test_silent_cases(self):
        silent = [
            self.bash("ls -la", {"stdout": "https://github.com/o/r/issues/3"}),
            self.bash("gh issue create -t x", {"stdout": "", "stderr": "GraphQL: error", "exit_code": 1}),
            self.bash("gh pr merge 7", {"output": "failed", "exit_code": 1}),
            self.bash("gh issue create --web", {"stdout": "https://github.com/o/r/issues/3"}),
            self.bash(f"python3 {CLI} create --type bug --title x",
                      {"stdout": "https://github.com/o/r/issues/3"}),
            self.bash("gh issue view 3", {"stdout": "https://github.com/o/r/issues/3"}),
            {"tool_name": "Edit", "tool_input": {"file_path": "x"}},
        ]
        for i, payload in enumerate(silent):
            with self.subTest(i=i):
                self.assertIsNone(self.hook(payload))
        self.assertIsNone(self.hook("not json"))
        self.assertIsNone(self.hook(self.bash("gh issue create -t x", "https://github.com/o/r/issues/3"),
                                    env={"GH_PROMAN_DISABLE": "1"}))

    def test_repeat_delivery_is_suppressed(self):
        payload = self.bash("gh issue create -t x", "https://github.com/o/r/issues/3", tool_use_id="t1")
        self.assertIsNotNone(self.hook(payload))
        self.assertIsNone(self.hook(payload))

    def test_mcp_tools(self):
        cases = [
            ("mcp__github__create_issue", {"owner": "o", "repo": "r", "title": "x"},
             {"content": [{"type": "text", "text": json.dumps({"html_url": "https://github.com/o/r/issues/3"})}]},
             "opened issue o/r#3"),
            ("mcp__github__issue_write", {"method": "create", "owner": "o", "repo": "r"},
             [{"type": "text", "text": "https://github.com/o/r/issues/3"}], "opened issue o/r#3"),
            ("mcp__codex_apps__github_create_issue", {"owner": "o", "repo": "r"},
             {"url": "https://github.com/o/r/issues/3"}, "opened issue o/r#3"),
            ("mcp__codex_apps__github_create_pull_request", {"owner": "o", "repo": "r", "body": "nothing"},
             {"url": "https://github.com/o/r/pull/7"}, "links no issue"),
            ("mcp__codex_apps__github_merge_pull_request", {"owner": "o", "repo": "r", "pull_number": 7},
             {"merged": True}, "you just merged PR https://github.com/o/r/pull/7"),
            ("mcp__github__merge_pull_request", {"owner": "o", "repo": "r", "pullNumber": 7}, {"merged": True},
             "--event pr-merged"),
        ]
        for i, (tool, tin, tres, expected) in enumerate(cases):
            (self.root / ".git" / "gh-proman" / "hook-seen").unlink(missing_ok=True)
            with self.subTest(tool=tool):
                ctx = self.hook({"tool_name": tool, "cwd": str(self.root), "tool_input": tin, "tool_response": tres,
                                 "tool_use_id": f"mcp-{i}"})
                self.assertIsNotNone(ctx)
                self.assertIn(expected, ctx)
        self.assertIsNone(self.hook({"tool_name": "mcp__github__issue_write", "cwd": str(self.root),
                                     "tool_input": {"method": "update", "owner": "o", "repo": "r"},
                                     "tool_response": "https://github.com/o/r/issues/3"}))

    def test_review_regressions(self):
        # The created item's own html_url wins over URLs quoted in its body.
        issue_json = json.dumps({"html_url": "https://github.com/o/r/issues/10", "number": 10,
                                 "body": "dup of https://github.com/o/r/issues/3"})
        ctx = self.hook({"tool_name": "mcp__github__create_issue", "cwd": str(self.root), "tool_use_id": "r1",
                         "tool_input": {"owner": "o", "repo": "r"},
                         "tool_response": {"content": [{"type": "text", "text": issue_json}]}})
        self.assertIn("o/r#10", ctx)
        # Heredoc bodies with apostrophes and titles mentioning gh-proman still trigger.
        ctx = self.hook(self.bash("gh pr create -t 'Add gh-proman config' --body-file - <<'EOF'\nIt's done\nEOF",
                                  "https://github.com/o/r/pull/7", tool_use_id="r2"))
        self.assertIn("you just opened PR o/r#7", ctx)
        # Failed commands reported as text stay silent.
        self.assertIsNone(self.hook(self.bash("gh pr merge 7", "Process exited with code 1", tool_use_id="r3")))
        self.assertIsNone(self.hook(self.bash(
            "gh pr create -t x", "a pull request for branch \"x\" already exists:\nhttps://github.com/o/r/pull/7",
            tool_use_id="r4")))
        # Non-GitHub MCP servers with the same tool names are ignored.
        self.assertIsNone(self.hook({"tool_name": "mcp__linear__create_issue", "cwd": str(self.root),
                                     "tool_input": {"title": "x"}, "tool_use_id": "r5",
                                     "tool_response": "see https://github.com/o/r/issues/3"}))

    def test_other_repo_without_config_is_silent(self):
        ctx = self.hook(self.bash("gh issue create -R x/y -t t", "https://github.com/x/y/issues/1"))
        self.assertIsNone(ctx)

    def test_english_config(self):
        self.write_config(language="en")
        ctx = self.hook(self.bash("gh issue create -t x", "https://github.com/o/r/issues/3", tool_use_id="en"))
        self.assertIn("Write comments in English.", ctx)


# ---------------------------------------------------------------------------------------------- candidates & sync

class SyncTests(Case):
    def setUp(self):
        super().setUp()
        self.add_issue(2, "[Piano] Piano generale", "Elenco: o/r#10")
        self.add_issue(10, "[S03 · P0] Scadenza effettiva coerente", COLLEAGUE_BODY, status="In progress")
        self.add_issue(11, "Login con Google non funziona", "Il pulsante Google resta in attesa.\n- [ ] fix")
        self.add_issue(12, "Export CSV", "Vedi `src/export/csv.ts` per il formato.\n- [ ] colonne")

    def test_candidates_rank_linked_first_and_classify_links(self):
        self.add_pr(20, "[S03] Unifica le scadenze", body="Piano: o/r#2. Task: https://github.com/o/r/issues/10",
                    head="codex/s03-deadline", closing=["o/r#10"], files=["src/lib/matching-availability.ts"])
        cands = {c["ref"]: c for c in self.cj("candidates", "--pr", "o/r#20")["candidates"]}
        ranked = list(cands)
        self.assertEqual(ranked[0], "o/r#10")
        self.assertEqual(cands["o/r#10"]["link"], "closes")
        self.assertEqual(cands["o/r#2"]["link"], "refs")
        signals = {r["signal"] for r in cands["o/r#10"]["reasons"]}
        self.assertTrue({"linked", "task-id", "paths"} <= signals, signals)

    def test_closing_keyword_takes_one_ref_and_task_id_matches_lowercase_branch(self):
        self.add_pr(23, "Batch", body="Closes #11 and #12", head="codex/s03-deadline")
        cands = {c["ref"]: c for c in self.cj("candidates", "--pr", "o/r#23")["candidates"]}
        self.assertEqual(cands["o/r#11"]["link"], "closes")
        self.assertEqual(cands["o/r#12"]["link"], "refs")
        self.assertEqual(cands["o/r#10"]["link"], "closes")  # S03 from codex/s03-…

    def test_closing_keyword_and_path_overlap(self):
        self.add_pr(21, "CSV", body="Fixes #12", files=["src/export/csv.ts"])
        cands = {c["ref"]: c for c in self.cj("candidates", "--pr", "o/r#21")["candidates"]}
        self.assertEqual(cands["o/r#12"]["link"], "closes")
        self.assertIn("paths", {r["signal"] for r in cands["o/r#12"]["reasons"]})

    def test_sync_pr_opened_plans_status_block_and_comment_without_close(self):
        self.add_pr(20, "[S03] Unifica", body="Refs o/r#2", head="codex/s03", closing=["o/r#10"], draft=False)
        plan = self.cj("sync", "--source", "https://github.com/o/r/pull/20")
        self.assertEqual(plan["event"], "pr-opened")
        ops = [(a["op"], a["issue"]) for a in plan["actions"]]
        self.assertIn(("add-to-board", "o/r#20"), ops)
        self.assertIn(("set-status", "o/r#10"), ops)
        self.assertIn(("status-block", "o/r#10"), ops)
        self.assertNotIn(("set-status", "o/r#2"), ops)  # a plain mention never moves the plan issue
        self.assertEqual({n["issue"] for n in plan["needs_text"]}, {"o/r#10", "o/r#2"})
        self.assertEqual(plan["proposed_closes"], [])
        self.assertEqual(plan["nudges"], [])

    def test_sync_apply_runs_deterministic_actions(self):
        self.add_pr(20, "[S03] Unifica", head="codex/s03", closing=["o/r#10"])
        plan = self.cj("sync", "--source", "o/r#20", "--apply")
        self.assertTrue(all(r["ok"] for r in plan["applied"]), plan["applied"])
        self.assertEqual(self.item("o/r#10")["fields"]["Status"], "In review")
        self.assertEqual(self.item("o/r#20")["fields"]["Status"], "In review")
        self.assertIn("o/r#20 (aperta → dev)", self.issue(10)["body"])
        self.assertTrue(self.issue(10)["body"].endswith(COLLEAGUE_BODY))
        self.assertEqual(self.issue(10)["comments"], [])  # comments are the agent's to write

    def test_sync_nudges_when_pr_links_nothing(self):
        self.add_pr(22, "Refactor helpers", body="Cleanup")
        plan = self.cj("sync", "--source", "o/r#22")
        self.assertEqual(plan["nudges"][0]["kind"], "pr-links-no-issue")

    def test_sync_merge_stacked_is_partial_and_landed_proposes_close(self):
        self.add_pr(20, "[S03] Unifica", head="codex/s03", base="codex/s01", closing=["o/r#10"], state="MERGED",
                    merge_sha="a" * 40, merged_at="2026-10-20T10:00:00Z")
        plan = self.cj("sync", "--source", "o/r#20", "--event", "pr-merged")
        self.assertEqual(plan["source"]["landed_in"], [])
        self.assertEqual(plan["proposed_closes"], [])
        self.assertEqual(plan["needs_text"][0]["event"], "pr-merged-partial")
        self.state = self.load()
        self.state["repos"]["o/r"]["compare"]["dev..." + "a" * 40] = "behind"
        self.save()
        plan = self.cj("sync", "--source", "o/r#20", "--event", "pr-merged")
        self.assertEqual(plan["source"]["landed_in"], ["dev"])
        close = plan["proposed_closes"][0]
        self.assertEqual(close["issue"], "o/r#10")
        self.assertEqual([i["text"][:7] for i in close["open_items"]], ["Proroga"])

    def test_sync_refuses_merge_event_for_open_pr(self):
        self.add_pr(20, "x")
        self.cj("sync", "--source", "o/r#20", "--event", "pr-merged", expect=3)

    def test_issue_created_looks_for_duplicates(self):
        self.add_issue(13, "Login con Google non funziona più", "Il pulsante Google resta in attesa", board=False)
        plan = self.cj("sync", "--source", "o/r#13")
        self.assertEqual(plan["actions"][0], {"op": "add-to-board", "issue": "o/r#13", "status": "backlog",
                                              "priority": "P2"})
        self.assertEqual(plan["needs_judgment"][0]["ref"], "o/r#11")

    def test_branch_selector_resolves(self):
        self.add_pr(20, "x", head="feat/branch", closing=["o/r#11"])
        plan = self.cj("sync", "--source", "feat/branch")
        self.assertEqual(plan["source"]["ref"], "o/r#20")


# ---------------------------------------------------------------------------------------------- close

class CloseTests(Case):
    def setUp(self):
        super().setUp()
        self.add_issue(10, "[S03 · P0] Scadenza", "## Criteri di chiusura\n- [ ] Proroga valida\n- [ ] Test ISO",
                       status="In review")
        self.add_pr(20, "[S03] Unifica", head="codex/s03", base="dev", closing=["o/r#10"], state="MERGED",
                    merge_sha="b" * 40, merged_at="2026-10-20T10:00:00Z")
        self.cmap = self.dir / "map.json"
        self.cmap.write_text(json.dumps({"Proroga valida": "o/r#20 resolver", "2": "test_iso in o/r#20"}))

    def close(self, *extra, expect=0):
        return self.cj("close", "--issue", "o/r#10", "--evidence", "https://github.com/o/r/pull/20",
                       "--comment", "Scadenze unificate.", *extra, expect=expect)

    def test_refuses_unmapped_criteria(self):
        out = self.close(expect=3)
        self.assertIn("open checklist item without evidence", out["refused"])
        self.assertEqual(self.mutations(), [])

    def test_closes_with_comment_first_then_done(self):
        res = self.close("--criteria-map", str(self.cmap))
        self.assertEqual(res["action"], "closed")
        muts = self.mutations()
        self.assertEqual(muts[0], "POST repos/o/r/issues/10/comments")
        self.assertEqual(muts[1], "GhpCloseIssue")
        issue = self.issue(10)
        self.assertEqual(issue["state"], "CLOSED")
        self.assertEqual(issue["state_reason"], "COMPLETED")
        self.assertEqual(self.item("o/r#10")["fields"]["Status"], "Done")
        body = issue["comments"][0]["body"]
        self.assertIn("**Chiusa da gh-proman** (completata).", body)
        self.assertIn("- [x] Proroga valida — o/r#20 resolver", body)
        self.assertIn("riapri l'issue", body)
        self.assertIn("Criteri coperti: 2/2", issue["body"])

    def test_refuses_plain_mention_and_heuristics(self):
        self.add_pr(21, "Unrelated title", body="Refs o/r#10", state="MERGED", base="dev", merge_sha="c" * 40,
                    merged_at="2026-10-20T10:00:00Z")
        out = self.cj("close", "--issue", "o/r#10", "--evidence", "o/r#21", "--comment", "x",
                      "--criteria-map", str(self.cmap), expect=3)
        self.assertIn("no closing link", out["refused"])

    def test_refuses_stacked_pr_not_landed(self):
        pr = self.state["repos"]["o/r"]["pulls"]["20"]
        pr["base"] = "codex/s01"
        self.save()
        out = self.close("--criteria-map", str(self.cmap), expect=3)
        self.assertIn("stacked", out["refused"])

    def test_refuses_recent_foreign_comment_until_acknowledged(self):
        self.state["repos"]["o/r"]["issues"]["10"]["comments"] = [
            {"id": 5, "user": {"login": "colleague"}, "created_at": "2026-10-21T08:00:00Z", "body": "Aspetta"}]
        self.save()
        out = self.close("--criteria-map", str(self.cmap), expect=3)
        self.assertIn("comment(s) from others", out["refused"])
        self.close("--criteria-map", str(self.cmap), "--ack-activity")

    def test_refuses_when_tracked_pr_not_landed(self):
        self.add_pr(30, "Dashboard side", slug="o/other", base="main")
        body = self.state["repos"]["o/r"]["issues"]["10"]["body"]
        self.state["repos"]["o/r"]["issues"]["10"]["body"] = (
            '<!-- gh-proman:status:start -->\n<!-- gh-proman:state {"prs":[{"ref":"o/other#30","state":"open",'
            '"base":"main"}],"v":1} -->\n<!-- gh-proman:status:end -->\n\n' + body)
        self.save()
        out = self.close("--criteria-map", str(self.cmap), expect=3)
        self.assertIn("o/other#30", out["refused"])

    def test_refuses_when_tracked_pr_merged_but_not_landed(self):
        self.add_pr(31, "Dashboard side", slug="o/other", base="feat/x", state="MERGED", merge_sha="9" * 40,
                    merged_at="2026-10-20T10:00:00Z")
        body = self.state["repos"]["o/r"]["issues"]["10"]["body"]
        self.state["repos"]["o/r"]["issues"]["10"]["body"] = (
            '<!-- gh-proman:status:start -->\n<!-- gh-proman:state {"prs":[{"ref":"o/other#31","state":"merged",'
            '"base":"feat/x","landed":[]}],"v":1} -->\n<!-- gh-proman:status:end -->\n\n' + body)
        self.save()
        out = self.close("--criteria-map", str(self.cmap), expect=3)
        self.assertIn("o/other#31", out["refused"])

    def test_teammate_agent_comment_counts_as_activity(self):
        self.state["repos"]["o/r"]["issues"]["10"]["comments"] = [
            {"id": 6, "user": {"login": "colleague"}, "created_at": "2026-10-21T09:00:00Z",
             "body": "<!-- gh-proman:v1 event=note source=o/r#9 -->\nRestano i criteri 2-3"}]
        self.save()
        self.close("--criteria-map", str(self.cmap), expect=3)

    def test_dry_run_close_makes_no_mutations(self):
        res = self.close("--criteria-map", str(self.cmap), "--dry-run")
        self.assertEqual(res["action"], "would-close")
        self.assertEqual(self.mutations(), [])

    def test_duplicate_rules(self):
        self.add_issue(15, "Same as 10", author="me", created="2026-10-20T09:00:00Z")
        res = self.cj("close", "--issue", "o/r#15", "--reason", "duplicate", "--duplicate-of", "o/r#10",
                      "--comment", "Stesso lavoro di o/r#10.")
        self.assertEqual(res["action"], "closed")
        self.assertEqual(self.issue(15)["duplicate_of"], "I_o/r_10")
        self.add_issue(16, "Older and foreign", author="colleague", created="2026-09-01T09:00:00Z")
        out = self.cj("close", "--issue", "o/r#16", "--reason", "duplicate", "--duplicate-of", "o/r#10",
                      "--comment", "x", expect=3)
        self.assertIn("older", out["refused"])
        self.assertIn("someone else", out["refused"])

    def test_not_planned_needs_no_criteria_map(self):
        res = self.cj("close", "--issue", "o/r#10", "--reason", "not_planned", "--comment", "Superata da o/r#30.")
        self.assertEqual(res["action"], "closed")
        self.assertEqual(self.issue(10)["state_reason"], "NOT_PLANNED")

    def test_disabled_autoclose(self):
        self.write_config(autoclose={"enabled": False})
        out = self.close("--criteria-map", str(self.cmap), expect=3)
        self.assertIn("disabled", out["refused"])

    def test_apply_enforces_close_limit(self):
        self.write_config(autoclose={"max_closes_per_run": 1})
        for n in (40, 41):
            self.add_issue(n, f"Dup {n}", created="2026-10-20T09:00:00Z")
        plan = [{"op": "close", "issue": f"o/r#{n}", "reason": "duplicate", "duplicate_of": "o/r#10",
                 "comment": "dup"} for n in (40, 41)]
        out = self.cj("apply", "--plan", "-", stdin=json.dumps(plan), expect=3)
        self.assertTrue(out["results"][0]["ok"])
        self.assertIn("closures in one run", out["results"][1]["refused"])
        self.assertEqual(self.issue(41)["state"], "OPEN")


# ---------------------------------------------------------------------------------------------- create & board

class CreateTests(Case):
    BODY = ("## Contesto\nIl login Google resta in attesa.\n\n## Come riprodurre\n1. Apri\n\n## Attività\n"
            "- [ ] Correggere\n\n## Criteri di chiusura\n- [ ] Login ok\n\n## Test e verifica\n- [ ] Test\n")

    def test_duplicate_guard_then_confirm_new(self):
        self.add_issue(11, "Login con Google resta in attesa", "Il login Google resta in attesa dopo il click.")
        self.cj("project", "setup")
        out = self.cj("create", "--type", "bug", "--title", "Login Google resta in attesa", "--body", self.BODY,
                      expect=3)
        self.assertEqual(out["candidates"][0]["ref"], "o/r#11")
        self.assertNotIn("POST repos/o/r/issues", self.mutations())
        res = self.cj("create", "--type", "bug", "--title", "Login Google resta in attesa", "--body", self.BODY,
                      "--priority", "P1", "--confirm-new")
        self.assertEqual(res["ref"], "o/r#12")
        self.assertEqual(res["lint_warnings"], [])

    def test_create_sets_board_fields_title_and_parent(self):
        self.state["projects"]["o/1"]["fields"][1]["options"] = [{"id": f"p{i}", "name": f"P{i}"} for i in range(4)]
        self.save()
        self.add_issue(1, "Piano")
        res = self.cj("create", "--type", "chore", "--title", "Ripulire CI", "--id", "S12", "--priority", "P0",
                      "--body", "## Contesto\nx\n\n## Attività\n- [ ] y\n\n## Verifica\n- [ ] z\n", "--parent", "#1",
                      "--confirm-new")
        self.assertEqual(res["title"], "[S12 · P0] Ripulire CI")
        item = self.item(res["ref"])
        self.assertEqual(item["fields"], {"Status": "Backlog", "Priority": "P0"})
        self.assertEqual(self.issue(1)["sub_issues"], [self.issue(2)["node_id"]])

    def test_missing_priority_option_fails_before_creating(self):
        out = self.cj("create", "--type", "chore", "--title", "x", "--body", "## Contesto\n- [ ] y", "--confirm-new",
                      expect=2)
        self.assertIn("project setup", out["error"])
        self.assertEqual(self.mutations(), [])

    def test_lint_blocks_branch_permalinks_and_secrets(self):
        body = self.BODY + "\nhttps://github.com/o/r/blob/main/src/a.ts#L3\n"
        out = self.cj("create", "--type", "bug", "--title", "x", "--body", body, "--confirm-new", expect=3)
        self.assertIn("branch 'main'", out["refused"])
        res = self.cj("lint-body", "--type", "bug", "--body", "token ghp_" + "a" * 36, expect=3)
        self.assertIn("secret", res["errors"][0])
        self.assertIn("missing section: ## Contesto", res["warnings"])

    def org_priority(self, no_read_org=False):
        self.state["orgs"] = {"o": {"no_read_org": no_read_org, "fields": [
            {"id": "IFSS_1", "name": "Priority", "options": [{"id": f"o{i}", "name": n} for i, n in
                                                            enumerate(["Urgent", "High", "Medium", "Low"])]}],
            "types": [{"id": "IT_1", "name": "Task"}, {"id": "IT_2", "name": "Bug"}, {"id": "IT_3", "name": "Feature"}]}}
        self.save()

    def test_org_issue_field_priority_and_issue_type(self):
        self.org_priority()
        res = self.cj("create", "--type", "bug", "--title", "Crash", "--priority", "P1", "--confirm-new",
                      "--body", self.BODY)
        issue = self.issue(1)
        self.assertEqual(issue["fields"], {"Priority": "High"})
        self.assertEqual(issue["type"], "Bug")
        self.assertEqual(res["board"]["priority"], {"level": "P1", "option": "High", "field": "issue:Priority"})
        self.assertNotIn("Priority", self.item("o/r#1")["fields"])
        self.cj("set-priority", "--issue", "o/r#1", "--priority", "P0")
        self.assertEqual(self.issue(1)["fields"], {"Priority": "Urgent"})
        setup = self.cj("project", "setup")
        self.assertEqual(setup["priority"]["map"], {"P0": "Urgent", "P1": "High", "P2": "Medium", "P3": "Low"})
        self.assertNotIn("GhpUpdateField", self.mutations())

    def test_priority_map_overrides_standard_names(self):
        self.org_priority()
        self.write_config(priority={"map": {"P3": "Medium"}})
        self.cj("create", "--type", "chore", "--title", "x", "--priority", "P3", "--confirm-new",
                "--body", "## Contesto\nx\n\n## Attività\n- [ ] y\n\n## Verifica\n- [ ] z\n")
        self.assertEqual(self.issue(1)["fields"], {"Priority": "Medium"})
        self.assertEqual(self.issue(1)["type"], "Task")

    def test_without_read_org_explains_instead_of_guessing(self):
        self.org_priority(no_read_org=True)
        out = self.cj("create", "--type", "bug", "--title", "x", "--priority", "P1", "--confirm-new",
                      "--body", self.BODY, expect=2)
        self.assertIn("read:org", out["error"])
        self.assertEqual(self.mutations(), [])
        doctor = self.cj("doctor", expect=1)
        prio = next(c for c in doctor["checks"] if c["name"] == "priority field")
        self.assertIn("read:org", prio["fix"])

    def test_set_status_never_moves_backwards(self):
        self.add_issue(1, "x", status="In review")
        self.assertEqual(self.cj("set-status", "--issue", "o/r#1", "--status", "in_progress")["action"], "skipped")
        self.assertEqual(self.cj("set-status", "--issue", "o/r#1", "--status", "in_progress",
                                 "--allow-backward")["action"], "set")

    def test_set_priority_updates_field_and_title_prefix(self):
        self.state["projects"]["o/1"]["fields"][1]["options"] = [{"id": f"p{i}", "name": f"P{i}"} for i in range(4)]
        self.save()
        self.add_issue(1, "[S03 · P2] Scadenze")
        self.cj("set-priority", "--issue", "o/r#1", "--priority", "P0")
        self.assertEqual(self.issue(1)["title"], "[S03 · P0] Scadenze")
        self.assertEqual(self.item("o/r#1")["fields"]["Priority"], "P0")

    def test_project_setup_adds_missing_options_and_keeps_ids(self):
        self.cj("project", "setup")
        names = [o["name"] for o in self.load()["projects"]["o/1"]["fields"][1]["options"]]
        self.assertEqual(names, ["P0", "P1", "P2", "P3"])
        self.state["projects"]["o/1"]["fields"][1]["options"] = [{"id": "keep", "name": "P0", "color": "RED",
                                                                  "description": ""}]
        self.save()
        res = self.cj("project", "setup")
        self.assertEqual(res["added_options"], ["P1", "P2", "P3"])
        opts = self.load()["projects"]["o/1"]["fields"][1]["options"]
        self.assertEqual(opts[0]["id"], "keep")
        self.assertEqual(self.cj("project", "setup")["added_options"], [])

    def test_doctor_reports_missing_priority_options(self):
        out = self.cj("doctor", expect=1)
        checks = {c["name"]: c for c in out["checks"]}
        self.assertTrue(checks["project"]["ok"])
        self.assertFalse(checks["priority field"]["ok"])
        self.assertIn("project setup", checks["priority field"]["fix"])


# ---------------------------------------------------------------------------------------------- audit

class AuditTests(Case):
    def setUp(self):
        super().setUp()
        self.commit("chore: init")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.base = self.commit("feat: unrelated")
        self.git("update-ref", "refs/remotes/origin/dev", "HEAD")

    def test_audit_finds_commits_and_prs_since_watermark(self):
        self.add_issue(10, "[S03 · P0] Scadenze", "Codice: `src/deadline.ts`\n- [ ] fatto",
                       created="2020-01-01T00:00:00Z")
        self.add_issue(11, "Nothing happened here", created="2020-01-01T00:00:00Z")
        self.add_issue(12, "Elsewhere", slug="o/other", body="Tocca https://github.com/o/r/issues/10")
        self.commit("fix(S03): scadenza comune\n\nCloses #10", "src/deadline.ts")
        sha = self.commit("docs: readme", "README.md")
        self.git("update-ref", "refs/remotes/origin/dev", "HEAD")
        self.add_pr(50, "Web merge", body="", state="MERGED", base="dev", merge_sha=sha,
                    merged_at="2026-10-20T10:00:00Z", files=["README.md"])
        self.state["repos"]["o/r"]["pulls"]["50"]["updated_at"] = "2026-10-20T10:00:00Z"
        self.save()
        out = self.cj("audit-scan", "--no-fetch")
        issues = {i["ref"]: i for i in out["issues"]}
        self.assertEqual(set(issues), {"o/r#10", "o/r#11", "o/other#12"})
        ev = issues["o/r#10"]["evidence"]
        self.assertEqual(issues["o/r#10"]["suggested"], "review-close")
        commit_ev = next(e for e in ev if e["kind"] == "commit")
        self.assertIn("closing-keyword", commit_ev["reasons"])
        self.assertEqual(commit_ev["landed_in"], ["dev"])
        self.assertEqual(issues["o/r#11"]["suggested"], "none")
        self.assertEqual([p["ref"] for p in out["unsynced_merged_prs"]], ["o/r#50"])

    def test_squash_merge_message_counts_for_the_pr(self):
        self.add_issue(10, "Scadenze", created="2020-01-01T00:00:00Z")
        sha = self.commit("Unifica scadenze (#51)\n\nCloses #10", "src/deadline.ts")
        self.git("update-ref", "refs/remotes/origin/dev", "HEAD")
        self.add_pr(51, "Unifica scadenze", state="MERGED", base="dev", merge_sha=sha,
                    merged_at="2026-10-20T10:00:00Z", files=["src/deadline.ts"])
        out = self.cj("audit-scan", "--no-fetch")
        ev = out["issues"][0]["evidence"]
        self.assertEqual([(e["kind"], e["ref"]) for e in ev], [("pr", "o/r#51")])  # not repeated as a commit
        self.assertTrue(ev[0]["closes"])
        self.assertEqual(out["unsynced_merged_prs"], [])

    def test_merged_pr_paging_stops_on_updated_at(self):
        self.add_issue(10, "Scadenze", created="2026-10-01T00:00:00Z")
        self.state["page_size"] = 1
        self.save()
        old = self.add_pr(40, "Old but recently commented", state="MERGED", base="dev", merge_sha="e" * 40,
                          merged_at="2025-01-01T00:00:00Z")
        old["updated_at"] = "2026-10-21T09:00:00Z"
        new = self.add_pr(41, "Fixes #10", body="Closes #10", state="MERGED", base="dev", merge_sha="f" * 40,
                          merged_at="2026-10-20T00:00:00Z")
        new["updated_at"] = "2026-10-20T00:00:00Z"
        self.save()
        out = self.cj("audit-scan", "--no-fetch")
        self.assertEqual([e["ref"] for e in out["issues"][0]["evidence"]], ["o/r#41"])

    def test_watermark_from_status_block_skips_covered_history(self):
        sha = self.commit("fix: Closes #10", "src/deadline.ts")
        self.git("update-ref", "refs/remotes/origin/dev", "HEAD")
        block = ('<!-- gh-proman:status:start -->\n<!-- gh-proman:state {"covered":{"o/r":{"sha":"%s",'
                 '"date":"2026-10-21T09:00:00Z"}},"v":1} -->\n<!-- gh-proman:status:end -->' % sha)
        self.add_issue(10, "Scadenze", block, created="2020-01-01T00:00:00Z")
        out = self.cj("audit-scan", "--no-fetch")
        self.assertEqual(out["issues"][0]["evidence"], [])
        self.assertEqual(out["issues"][0]["watermark"]["source"], "status block")


# ---------------------------------------------------------------------------------------------- misc

class MiscTests(Case):
    def test_italian_locale_does_not_change_output(self):
        self.env.update({"LC_ALL": "it_IT.UTF-8", "LANG": "it_IT.UTF-8", "LC_NUMERIC": "it_IT.UTF-8"})
        self.add_issue(1, "x", "body")
        self.cj("status-block", "--issue", "o/r#1", "--status", "ready", "--summary", "Avanzamento 0.5")
        self.assertIn("aggiornato 21/10/2026", self.issue(1)["body"])

    def test_permalink_requires_pushed_sha(self):
        self.commit("one", "src/a.ts")
        self.cj("permalink", "src/a.ts:3", expect=3)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        sha = self.git("rev-parse", "HEAD")
        self.assertEqual(self.cj("permalink", "src/a.ts:3-5")["url"],
                         f"https://github.com/o/r/blob/{sha}/src/a.ts#L3-L5")

    def test_install_dry_run(self):
        home = Path(self.env["HOME"])
        (home / ".codex").mkdir(parents=True)
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "install"), "--dry-run"], env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would link", proc.stdout)
        self.assertFalse((home / ".codex" / "skills" / "gh-proman").exists())

    def test_hooks_json_points_at_hook(self):
        data = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        commands = [h["command"] for m in data["hooks"]["PostToolUse"] for h in m["hooks"]]
        self.assertTrue(all("skills/gh-proman/scripts/hook.py" in c for c in commands))
        self.assertTrue(HOOK.is_file())


if __name__ == "__main__":
    unittest.main()
