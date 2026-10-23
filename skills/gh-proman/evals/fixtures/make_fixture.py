#!/usr/bin/env python3
"""Build an eval sandbox: a git repo with a gh-proman config and a fake `gh` whose state is a small board.

    python3 make_fixture.py <scenario> <dir>

Then run the agent inside <dir>/repo with `source <dir>/env.sh` (PATH puts the fake gh first, so nothing
reaches GitHub). Scenarios: setup, create-duplicate, sync-pr-opened, sync-merge-stacked, audit-close.
The grader reads <dir>/state.json and <dir>/gh.log afterwards.
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE_DIR = HERE.parents[3] / "tests" / "fakes"


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "Eval", "GIT_AUTHOR_EMAIL": "e@example.com", "GIT_COMMITTER_NAME": "Eval",
                        "GIT_COMMITTER_EMAIL": "e@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                        "HOME": str(root)})


def board():
    status = {"id": "F_S", "name": "Status", "dataType": "SINGLE_SELECT",
              "options": [{"id": f"s{i}", "name": n} for i, n in
                          enumerate(["Backlog", "Ready", "In progress", "In review", "Done"])]}
    priority = {"id": "F_P", "name": "Priority", "dataType": "SINGLE_SELECT",
                "options": [{"id": f"p{i}", "name": f"P{i}"} for i in range(4)]}
    return {"viewer": "me", "seq": 1000, "clock": 0, "now": "2026-10-21T10:00:00Z",
            "repos": {"acme/app": {"default_branch": "main", "branches": ["main", "dev"], "issues": {}, "pulls": {},
                                   "compare": {}, "commits": {}}},
            "projects": {"acme/1": {"id": "PVT_1", "title": "Acme", "items": [],
                                    "fields": [status, priority, {"id": "F_E", "name": "Estimate", "dataType": "NUMBER"}]}}}


def issue(state, n, title, body, status="Backlog", author="me", created="2026-10-01T09:00:00Z"):
    state["repos"]["acme/app"]["issues"][str(n)] = {
        "number": n, "node_id": f"I_{n}", "title": title, "body": body, "state": "OPEN", "author": author,
        "created_at": created, "updated_at": created, "comments": []}
    state["projects"]["acme/1"]["items"].append({"id": f"PVTI_{n}", "content": f"acme/app#{n}",
                                                 "fields": {"Status": status, "Priority": "P2"}})


def pr(state, n, title, body, head, base, closing=(), state_="OPEN", merge_sha=None, files=()):
    state["repos"]["acme/app"]["pulls"][str(n)] = {
        "number": n, "node_id": f"PR_{n}", "title": title, "body": body, "head": head, "base": base,
        "state": state_, "draft": False, "closing": list(closing), "files": list(files),
        "commits": [{"oid": "c" * 40, "headline": title}], "merge_sha": merge_sha,
        "merged_at": "2026-10-20T15:00:00Z" if state_ == "MERGED" else None}


def main():
    scenario, out = sys.argv[1], Path(sys.argv[2]).resolve()
    root = out / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "remote", "add", "origin", "https://github.com/acme/app.git")
    (root / "README.md").write_text("# app\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chore: init")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/dev", "HEAD")
    state = board()
    if scenario != "setup":
        (root / ".github").mkdir()
        (root / ".github" / "gh-proman.json").write_text(json.dumps(
            {"version": 1, "language": "it", "project": {"owner": "acme", "number": 1},
             "integration_branches": ["dev", "@default"]}, indent=2))
    if scenario == "create-duplicate":
        issue(state, 4, "Login con Google resta in attesa dopo il consenso",
              "## Contesto\nDopo il consenso Google la pagina resta in caricamento.\n- [ ] Correggere")
    if scenario in ("sync-pr-opened", "sync-merge-stacked", "audit-close"):
        issue(state, 7, "Esporta i report in CSV",
              "## Criteri di accettazione\n- [ ] Colonne visibili nell'ordine della tabella\n"
              "- [ ] Date in formato italiano\n\nCodice: `src/reports/table.tsx`", status="In progress")
        issue(state, 8, "Piano report Q4", "Attività: acme/app#7")
    if scenario == "sync-pr-opened":
        pr(state, 12, "feat(reports): export CSV", "Closes acme/app#7\nRefs acme/app#8", "feat/csv", "dev",
           files=["src/reports/table.tsx", "src/reports/csv.ts"])
    if scenario == "sync-merge-stacked":
        pr(state, 12, "feat(reports): export CSV", "Closes acme/app#7", "feat/csv", "feat/reports-base",
           state_="MERGED", merge_sha="d" * 40, files=["src/reports/csv.ts"])
    if scenario == "audit-close":
        (root / "src").mkdir()
        (root / "src" / "csv.ts").write_text("export {}\n")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "feat(reports): export CSV con date italiane\n\nCloses acme/app#7")
        git(root, "update-ref", "refs/remotes/origin/dev", "HEAD")
    if scenario == "estimate":
        issue(state, 9, "Esporta i report in CSV dal drawer Report",
              "## Contesto\nGli analisti copiano le tabelle a mano.\n\n## Attività\n- [ ] Server Action che genera il CSV\n"
              "- [ ] Pulsante \"Esporta CSV\" nel drawer Report\n- [ ] Docs IT/EN aggiornate\n\n"
              "## Criteri di accettazione\n- [ ] Colonne e filtri come la tabella visibile\n- [ ] UTF-8 con BOM per Excel\n")
    (out / "state.json").write_text(json.dumps(state, indent=1))
    (out / "env.sh").write_text(
        f'export PATH="{FAKE_DIR}:$PATH"\nexport GH_FAKE_STATE="{out / "state.json"}"\n'
        f'export GH_FAKE_LOG="{out / "gh.log"}"\nexport GH_PROMAN_CONFIG="{out / "user-config.json"}"\n'
        'export GH_PROMAN_NOW="2026-10-21T10:00:00Z"\nunset GITHUB_TOKEN GH_TOKEN\n')
    print(root)


if __name__ == "__main__":
    main()
