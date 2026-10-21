# Linking issues, commits and PRs

## Why GitHub doesn't close these issues by itself

GitHub closes an issue from `Closes #N` only when the PR merges into the repository's **default** branch. On this team, work merges into integration branches first:
- `dev` here;
- `dev-deb` for the Redigo–Dashboard plan;
- often stacked drafts on top of each other (`codex/s03-…` based on `codex/s01-…`).

So `Closes` keywords sit unused until a release reaches `master`, sometimes weeks later, and the board says "In progress" for work that's done. gh-proman decides instead:
- **opening** a PR moves the issues it closes to In review (In progress for drafts) and comments;
- **merging** into an integration branch, or a stacked merge whose commit later reaches one, closes the issues it completes (rubric);
- an **audit** catches merges done in the web UI and commits pushed without a PR.

## The convention

| Where | Related work | Completes the issue |
|---|---|---|
| Commit body | `Refs owner/repo#N` | `Closes owner/repo#N` (only in the commit that finishes it) |
| PR body | `Refs owner/repo#N` | `Closes owner/repo#N` |
| PR title / branch | task ID when the issue has one: `fix(S03): …`, `codex/s03-…` | same |
| Web UI | Development sidebar → link the issue | same |

- Use the full `owner/repo#N` form. Cross-repo work is normal here, and a bare `#N` always means the PR's own repo.
- One line per issue. A PR that finishes one issue and touches another: `Closes …#10` and `Refs …#2`.
- The plan or umbrella issue gets `Refs`, never `Closes`.

## How gh-proman detects links

From strongest to weakest, as reported in `candidates` / `sync` reasons:

| Signal | Counts as | Effect |
|---|---|---|
| `linked`: Development sidebar or a closing keyword GitHub parsed | closing link | Status moves; close possible on landing |
| `closing-keyword`: `Closes/Fixes/Resolves/Chiude/Risolve <ref>` in title, body or commits | closing link | same |
| `task-id`: the issue's `[S03 · …]` ID in the PR title, branch or commit subjects | closing link | same |
| `explicit-ref`: any other mention of the issue | mention | comment only, no Status change |
| `mentioned-by-issue`: the issue body names this PR or branch | judgment | listed for you to judge |
| `paths`: files the issue links to are changed | judgment | listed for you to judge |
| `task-id-body`: the ID appears only in the PR body | judgment | listed for you to judge |
| `text` / `title`: similar wording (TF-IDF) | judgment | duplicates and related work |

## The hook nudge

After `gh pr create`, the hook checks the PR body you passed. If it links no issue, it asks you to add one with `gh pr edit <url> --body-file <file>`. With `--fill`, the body isn't known at that point, so `sync` checks it and reports a `pr-links-no-issue` nudge.
