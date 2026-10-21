# Filing review findings (forge)

Forge's reviewer labels every finding `- [P0..P3][CONFIRMED|PLAUSIBLE] file:line — what breaks (input)`. Some findings never block a run, and without gh-proman they stay in forge's run directory where nobody looks:

| Source | What it holds | Origin label |
|---|---|---|
| `<plan>/tasks/<id>/known_issues.md` | CONFIRMED findings milder than `--qa-threshold`: the task merged anyway | `tolerated` |
| `<plan>/tasks/<id>/qa.last` | the last review, including PLAUSIBLE findings, which never block | `plausible` (with `--include-plausible`) |
| `<plan>/final-review/qa.last`, `astra-final-review/qa.last`, … | a whole-run review: everything it found that wasn't fixed afterwards | `review` (CONFIRMED) / `plausible` |

`findings --from` accepts any of these:
- a plan directory: it walks `tasks/`;
- a task, solo-run or review directory;
- a single `known_issues.md` or `qa.last`.

A task's findings are used only when it passed (`PASS`, or `MERGED` in `results.tsv`). A failing task's findings are still blocking work, and forge is handling them.

## What an issue looks like

- **Title:** `<file>: <first sentence of the finding>`, cut at about 90 characters. PLAUSIBLE ones start with `Da verificare:` / `To verify:`.
- **Type:** Bug.
- **Priority:** the forge severity. P0–P3 map straight onto gh-proman's levels, and onto the org's Urgent…Low where Priority is an organisation issue field.
- **Body:** the bug template in the config language:
  - why it's open (tolerated under threshold X, a plausible finding to verify, or left over from the whole-run review);
  - the origin: run, task, reviewer spec and commit;
  - the finding quoted verbatim;
  - a short checklist (reproduce, fix, regression test; for PLAUSIBLE: confirm or refute);
  - a permalink at the merged commit, or the plain `file:line` when that commit isn't on GitHub yet.
- **Fingerprint:** each finding gets a hidden `<!-- gh-proman:finding fp=<12 hex> source=forge:<plan>/<task> -->`. The hash covers repo, file and finding text. Re-running `findings` on the same run, or on a later run that repeats a finding, reports it under `already_filed` and creates nothing. Forge files findings itself with `forge issues` where gh-proman isn't set up, marked `<!-- forge:finding fp=… -->` with the same hash; those count as filed too, on the board or not.

## Choosing what to file

| Option | Use when |
|---|---|
| default (one issue per finding) | a handful of findings, or P0–P2 that deserve their own tracking |
| `--group task` | many small findings from one task |
| `--group run` | a long tail of P3s: one checklist issue for the whole run |
| `--min-severity P2` | file only P0–P2 |
| `--include-plausible` | also file the low-confidence ones, as "to verify" |

`--apply` creates at most `--max-issues` (default 10) per invocation. Likely duplicates of existing issues are skipped and listed with their candidates: comment on the existing issue instead, or pass `--confirm-new` after checking.

## Example (Italian config)

```
$ gh-proman findings --from ~/forge-runs/lead-import --include-plausible --json
summary: 4 findings · 3 tolerated · 1 plausible · 0 already filed
drafts:
  P2  company-import.ts: Company creation occurs before replaying the profile receipt…
  P3  company-import.ts: Persisting targetListId: null makes a list creation race unrecoverable
  P3  lead-import-review.ts: The saved company name remains forced after editing the identity
  P3  Da verificare: lead.ts: Lead names may keep trailing spaces
notes: ui: status FAIL, its findings still block it
```

The agent shows this, asks once, then runs the same command with `--apply`.
