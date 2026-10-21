---
name: gh-proman
description: Use when the team tracks work on a GitHub Project board and the user wants to open, file or track an issue or ticket, plan tasks on the board, check whether an issue already exists, move an issue's status or priority, or update or close issues after a PR. Also use it right after a gh-proman hook message (it appears after `gh issue create`, `gh pr create` or `gh pr merge`, or the GitHub MCP equivalents), and when asked to audit or reconcile the board with a repository ("what's already done?", "aggiorna le issue", "allinea il progetto", "which issues are stale?"). Keeps every agent's issues consistent and in sync: templates per type, a duplicate guard, context comments on related issues, a status block in each issue, and auto-close at high confidence when a merged PR completes an issue. Not for Linear, Jira or Trello, and not for plain git or code questions with no issue-tracking intent.
---

# gh-proman

The team's tasks live as GitHub issues on a GitHub Project board, and several personal agents (Claude Code, Codex) work on them in parallel. Each agent only knows what it did itself, so unless every one of them writes back to the issues, they drift apart. gh-proman makes the issues the shared memory:
- every new issue is well formed and on the board;
- every issue or PR an agent creates, and every merge, updates the related issues;
- an audit catches what happened without an agent, such as PRs merged in the web UI.

**Division of labour.** You judge: which issues relate, what is worth saying, whether an issue is really done. The script does all GitHub and git I/O deterministically: idempotent markers, guards, JSON. `<skill_dir>` is the directory containing this SKILL.md:

    python3 <skill_dir>/scripts/gh_proman.py <command> [--json] [--dry-run] [-C <repo>]

Every write command supports `--dry-run`. Exit code 3 means a guard refused; the JSON says why and what to do instead. Never work around a refusal with raw `gh` calls.

## Preflight (once per session, before the first gh-proman task)

Run `doctor`. It checks:
- the gh version;
- which token is active and its scopes (`repo` + `project`);
- the config;
- the board and its Status and Priority fields;
- the integration branches.

Relay any `fix` lines to the user. Common ones:
- **Bad credentials from an env token.** An exported `GITHUB_TOKEN` overrides the keyring login. The user should unset or replace it; `gh auth refresh` does not help in that case.
- **Missing `project` or `read:org` scope:** `gh auth refresh -s project,read:org`. The user runs it themselves with `! gh auth refresh -s project,read:org`. Without `read:org`, an organisation's Priority issue field and issue types are invisible.

Then run `config show` to learn the board, the language and the integration branches.

## Setup (no config for this repo)

1. **Ask first, then stop until the user answers.** The config decides where every agent on the team writes, so a wrong guess is costly. Ask in one message:
   - which board (`https://github.com/orgs/<owner>/projects/<n>`);
   - the language for issues and comments (`it` or `en`);
   - the integration branches whose merges count as done (e.g. `dev`, `dev-deb`, plus the default branch);
   - whether the config goes in the repo (`.github/gh-proman.json`, shared with the team) or in the user's profile (`--user`, matched by owner).
2. Run `config init --owner <o> --number <n> --language <l> --branches dev,@default [--user]`.
3. Run `project setup --dry-run` and show the user what it will do:
   - When Priority is an organisation issue field (e.g. Urgent/High/Medium/Low), nothing is created; it shows how P0–P3 map to the org's options.
   - Otherwise it adds the options P0–P3 to the board's Priority field.

   Run it for real once they agree. It never rewrites Status options and keeps existing options' ids.
4. Run `doctor` again. `references/config.md` has every key, including how to point a repo at an external client's board.

## Create an issue

1. Pick the type (`bug`, `feature`, `chore`) and read `assets/templates/<language>/<type>.md`. Fill every section and drop the `<!-- hints -->`. `references/issue-writing.md` covers:
   - titles, including the `[ID · Pn]` prefix for planned tasks;
   - how concrete the checklists should be;
   - the house style already used on the board.
2. Link code with `permalink <path>[:line[-line]]`. It refuses unpushed SHAs, and branch links rot. Link related issues as `Refs owner/repo#N`.
3. Write the body to a file and run `lint-body --type <type> --body-file <file>`. Fix the errors; the warnings are judgment calls.
4. Run `create --type <type> --title "<title>" --body-file <file> --priority P<n> [--id S12] [--parent <issue>]`. It:
   - sets the organisation's issue type (Bug, Feature or Task) when the org has one;
   - adds the issue to the board with Status Backlog;
   - sets the priority, on the org issue field or the board field, wherever Priority lives.
5. **Duplicate guard.** If `create` exits 3 with candidates, show them to the user. Offer to comment on or extend the existing issue instead (`comment --event issue-related`, or ask the owner). Pass `--confirm-new` only after the user says it really is new.

Several related tasks: create a parent issue and its children with `--parent`, so the board shows sub-issue progress.

## Sync (after a hook message, or whenever you created/merged something without one)

The hook tells you to run `sync --source <url> --apply --json`. Without a hook (another tool created it, or the hook isn't installed), run the same command yourself after every issue you create, PR you open and PR you merge.

`sync` plans; `--apply` runs only the deterministic part (board membership, Status, status block). The rest is yours:

- **`needs_text`**: write one comment per entry with `comment --issue <i> --event <e> --source <s> [--sha <sha>] --body-file <f>`, in the config language. Say:
  - what changed for that issue, with a link;
  - what remains;
  - anything another agent picking it up must know.

  Three to six lines. No restating the issue, no file-by-file diff.
- **`needs_judgment`**: candidates found by weaker signals (shared paths, task IDs in the body, similar wording). For each, decide whether it is unrelated, related, a duplicate, or covered by this work:
  - unrelated: do nothing;
  - related: an `issue-related` comment only if it changes something for that issue;
  - duplicate of an older issue: follow the duplicate rules in `references/rubric.md`.

  Most candidates need nothing; silence is fine.
- **`proposed_closes`** (only after a merge that landed in an integration branch): close only if the PR completes the issue, per `references/rubric.md`.
  - Map every open checklist item to evidence in a criteria-map JSON file.
  - Write the closing comment.
  - Run `close --issue <i> --evidence <pr-url> --criteria-map <f> --comment-file <c>`.
  - If anything remains, comment instead (`--event pr-merged`) and say what's left. The script refuses unsafe closes; a refusal means comment instead.
- **`nudges`**: when the PR links no issue, add `Refs owner/repo#N` (or `Closes …` when it completes the issue) to its body with `gh pr edit`.

You can batch the writes in one `apply --plan <file>` (a list of actions). Limits apply per run; see `references/rubric.md`.

Never close anything when a PR is only opened. Draft and stacked PRs (base = another feature branch) close nothing until their work reaches an integration branch.

## Audit (on request: "audit", "what's already done?", "aggiorna le issue")

1. In the repository checkout, run `audit-scan --json`. Pass `--no-fetch` if the user said not to touch the network for git. It returns:
   - every open board issue tied to this repo;
   - each issue's watermark: the code already checked, taken from its status block or its last gh-proman marker;
   - evidence since the watermark: merged PRs and commits, with reasons, and whether they `landed_in` an integration branch;
   - `unsynced_merged_prs`: PRs merged with no gh-proman trace, usually merged in the web UI.
2. For each issue decide one of:
   - **done**: `suggested: review-close`, judged with the rubric;
   - **needs an update**: comment with `--event audit --source <owner/repo>@<branch> --sha <head sha>`;
   - **nothing new**.

   Read the evidence yourself (`git show`, the PR) when the reasons are only paths.
3. Show the user one summary: issues to close (with the evidence), issues to comment on, unsynced PRs. **Ask once before closing anything during an audit**, unless the user already said to close what's done. Comments and status blocks don't need approval.
4. Apply. Then record the check on every issue you handled with `status-block --issue <i> --covered <owner/repo>=<head sha>`, so the next audit starts from there. Treat each unsynced PR as a `sync --source <pr> --event pr-merged`.

## Linking convention (commits and PRs)

- Commits: put `Refs owner/repo#N` in the body. Use `Closes owner/repo#N` only in the commit that completes the issue.
- PR bodies: one `Refs` / `Closes` line per issue. Also link the issue in the PR's Development sidebar when you open it from the web.
- Task IDs: put the board's task ID in the PR title or branch when the issue has one, e.g. `fix(S03): …` or `codex/s03-…`. gh-proman treats it as a closing link.

`references/linking.md` explains why GitHub's own auto-close rarely fires here (PRs merge into `dev`, not the default branch), and how gh-proman decides instead.

## Approval gates

| Ask first | No need to ask |
|---|---|
| `config init`, `project setup` (show `--dry-run`) | `doctor`, `config show`, `candidates`, `sync`, `audit-scan`, any `--dry-run` |
| `create --confirm-new` past the duplicate guard | `create` when the guard passes and the user asked for the issue |
| closing during an audit (once, for the whole list) | sync comments, status blocks, Status moves |
| closing an issue someone else opened and is actively discussing | closing after a merge when the rubric is met (hook-driven) |
| `--allow-backward`, editing anyone's issue text | |

## Common mistakes

- Closing because a PR was opened, or because a stacked PR merged into another feature branch.
- Closing on a plain mention (`Refs`), shared paths or similar wording. Those earn a comment at most.
- Editing or ticking checkboxes in someone's issue body. Only the gh-proman status block is yours; evidence goes in your comment.
- One comment per commit, or comments that restate the diff. One per event, short, about the issue.
- Comments in the wrong language. Use the config `language`, even if you're chatting with the user in another.
- Creating a new issue when one already covers the work. Extend it with a comment instead.
- Branch permalinks (`/blob/main/...`). Use `permalink`.
- Retrying a refused `close` with raw `gh issue close`.

## References

- `references/config.md`: config keys, resolution order (repo file, user profile), external boards, disabling.
- `references/issue-writing.md`: templates, titles and prefixes, checklists, the team's house style.
- `references/comments.md`: comment formats per event, markers, the status block, examples in Italian and English.
- `references/rubric.md`: auto-close rubric, duplicates, per-run limits, what the script enforces. Read it before any close.
- `references/linking.md`: Refs/Closes, stacked PRs, integration branches, how links are detected.
