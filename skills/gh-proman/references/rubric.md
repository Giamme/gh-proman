# Auto-close rubric

Closing is the one action other agents can't easily see coming, and a wrong close hides work. gh-proman therefore closes only at high confidence. The script enforces the checks it can verify; you judge the rest before calling `close`.

## A merged PR completes an issue (`--reason completed`)

All of these must hold. The script checks 1–4 and 6; you check 5 and the substance.

1. **Closing link.** The PR is tied to the issue by one of:
   - the Development sidebar link;
   - `Closes`/`Fixes`/`Resolves` (or `Chiude`/`Risolve`) in the PR body or a commit;
   - the issue's task ID in the PR title or branch (`fix(S03): …`, `codex/s03-…`).

   A plain mention (`Refs`, a URL in passing), shared file paths or similar wording are never enough. They get a comment.
2. **Landed.** The PR merged into an integration branch (`integration_branches` in the config), or merged into a stacked base whose merge commit has since reached one. A merge into another feature branch gets a `pr-merged-partial` comment.
3. **Every open checkbox has evidence.** Every unchecked `- [ ]` in the issue body, in any section, needs an entry in `--criteria-map`: the item's text (or its 1-based position) mapped to evidence. Evidence is a PR, a commit, a test name, or "n/a: <why it no longer applies>". The script refuses if one is missing. It never ticks the boxes; your mapping appears in the closing comment.
4. **Every tracked PR landed.** PRs listed in the issue's status block, such as the Dashboard half of a coordinated change, must be merged and landed too.
5. **No open gate the evidence doesn't cover.** Read the issue. If it requires something the PR can't provide, comment, set `in_review` and leave it open:
   - acceptance or functional review ("collaudo", "revisione funzionale", "accettazione");
   - a closing receipt (e.g. "Ricevuta di chiusura" / `SCHEDA_CHIUSURA.md`);
   - a deploy or production check;
   - a decision by a named person.
6. **No unread activity.** A comment from someone else in the last 24 hours blocks the close until you've read it and pass `--ack-activity`.

When in doubt, don't close: write a `pr-merged` comment that says what landed and what's left. A missed close costs one click; a wrong one hides work from the whole team.

## Duplicates (`--reason duplicate --duplicate-of <issue>`)

The script closes only:
- the **newer** issue;
- one opened by **you** (the authenticated user);
- one with **no comments from others**.

In any other case, post a `duplicate-candidate` comment on both issues, linking each other, and let the owners decide. Before closing as a duplicate, move anything useful from the newer issue into a comment on the older one.

## Not planned (`--reason not_planned`)

Only when the user says so, or when a newer issue/PR explicitly states that it replaces this one. Quote that statement in the comment.

## Order, limits and undo

- The closing comment is written **before** the issue is closed, then Status moves to Done and the status block is updated.
- `max_closes_per_run` (default 3) and `max_actions_per_run` (default 20) apply per process: one `apply` plan or one command. More than that means ask the user.
- Every closing comment ends with how to undo it: reopen the issue, and the next sync takes it into account.
- `--dry-run` shows the whole sequence without writing.
- During an **audit**, list the closes and ask the user once before applying. After a **merge** (hook-driven sync), close without asking when the rubric holds.

## Cross-repo

Closing an issue in another repository is allowed when it is on the same board and the PR has a closing link to it (point 1). Issues outside the board get a comment only.
