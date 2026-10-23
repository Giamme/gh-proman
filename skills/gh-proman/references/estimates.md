# Estimates

An estimate on this board is **hands-on human time, in hours**: the time a person spends briefing agents, steering them, reviewing their diffs and verifying the result. Agents do the typing, and they work on several items of the same issue at once, so the usual estimate (sum the checklist items as if one person did them in order) comes out several times too high.

The estimate goes in the board's Number field `Estimate` (config `estimate.field`), with the breakdown in the gh-proman status block. The script does the arithmetic and the calibration. You do the judging: which items run in parallel and what each lane costs a person.

## Method

1. **Read the anchors first.** `estimate --issue <i> --json` returns up to five similar closed issues with the hours they actually took. Start from the closest one and adjust it, instead of reasoning from scratch. With no anchors (a new board), use the guide below.
2. **Group the open checklist items into lanes.** A lane is work one agent can carry on its own while others run: separate files or layers, no output another lane needs first. Items that depend on each other go in the same lane. Every open item goes in exactly one lane. The script refuses a plan that misses an item or lists one twice.
3. **Price each lane in human minutes, not agent minutes.**
   - Count the steering prompts, the reading of the diff, the fix-up rounds you expect, and the checks a person runs.
   - Don't count the agent's run time, unless the person is blocked waiting and can't do anything else.
   - Review time grows more slowly than lane size: a second similar item in the same lane adds far less than the first.
4. **Add the fixed costs once per issue:**
   - `brief_hours`: reading the issue and the code, and writing the briefs;
   - `integration_hours`: merging the lanes, the end-to-end check on dev, and the PR.
5. **Never sum per-item implementation time.** The total is `brief + Σ lanes + integration`. The script multiplies it by the team's calibration factor and snaps it to the scale (0.5, 1, 2, 3, 5, 8, 13 h).

Above 13 h (`estimate.max_hours`) the result carries a warning. Suggest splitting the issue into sub-issues (`create --parent`) instead of defending a big number.

### Guide for when there are no anchors yet

| Work, per lane | Hands-on time |
|---|---|
| Brief for a well-specified change | 5–15 min |
| Review of a small, focused diff (one area, < 200 lines) | 10–20 min |
| Review of a broad diff, or one in code few people know | 30–60 min |
| One fix-up round after review | 10–20 min |
| Manual check on dev or stage of one flow | 10–30 min |
| Data migration or prod-touching step, done by hand | price as the full real time |

These are starting points. Once a few estimated issues close, the anchors and the factor replace them.

## Plan format

`estimate --apply --plan <file>` takes a list. Items are 1-based positions in the issue's `open_items` (as `estimate --json` numbers them) or their exact text:

```json
[
  {"issue": "acme/app#42", "brief_hours": 0.25, "integration_hours": 0.5,
   "lanes": [
     {"name": "server", "items": [1, 4, 5], "hours": 0.5},
     {"name": "ui", "items": [2], "hours": 0.25},
     {"name": "docs", "items": [3], "hours": 0.25}]}
]
```

Before writing anything, the script checks every entry: hours above 0 for each lane, and every open item placed exactly once. It writes the board field and the status block line ("Stima: 2 h · 3 lane · fattore 1.0"). Re-running with the same result is a no-op. An `Estimate` that someone set by hand is refused unless you pass `--overwrite` after asking the user. Re-estimating a value gh-proman set itself needs no flag.

## Example

Feature "Export CSV dal drawer Report", five open items: Server Action that builds the CSV, "Esporta CSV" button, IT/EN docs, columns and filters match the table, UTF-8 with BOM.

- **Naive, sequential:** 3 h + 2 h + 1 h + 2 h + 1 h = **9 h**.
- **Lanes, hands-on:**
  - brief 0.25 h;
  - server lane (Server Action, columns/filters, BOM: one agent, one diff to review) 0.5 h;
  - UI lane (button) 0.25 h;
  - docs lane 0.25 h;
  - integration and a check on dev 0.5 h.

  That makes 1.75 h, which snaps to **2 h**.

The same in English for an English board: the lane names and hours are the same, only the block line reads "Estimate: 2 h · 3 lanes · factor 1.0".

## Actuals and calibration

The factor is `median(actual / raw estimate)` over closed (completed) issues that have both values. `estimate --calibration --json` lists them.

- **Reported actuals** are the most reliable: `estimate --issue <i> --actual <hours>`. Record one whenever the user says how long an issue really took. Don't ask after every close.
- **Git-proxy actuals** fill the gaps automatically. The script reads the commits of the issue's merged closing PRs and groups them into sessions: commits less than `session_gap_minutes` (60) apart are one session. Each session counts its span plus `session_lead_minutes` (15) for the work before its first commit. This follows the time a person was present, not the days the PR stayed open. It is rougher than a reported actual: it misses review time after the last commit.
- Below `min_samples` (5) usable samples the factor is `prior_factor` (1.0). Once there are 5 reported actuals, only reported ones count. Before that, git-proxy ones fill in, and `basis` says so.
- The factor is clamped to 0.25–4, so a few odd issues can't distort every future estimate.

The factor corrects the raw number, so calibrate against the raw estimate (stored in the block), not the snapped one. Don't adjust your raw numbers to "pre-apply" the factor; the script does that.

## Config

| Key | Default | Meaning |
|---|---|---|
| `estimate.field` | `Estimate` | Board Number field for the hours. `project setup` creates it if missing |
| `estimate.scale` | 0.5, 1, 2, 3, 5, 8, 13 | Values estimates snap to (nearest on a log scale) |
| `estimate.max_hours` | 13 | Above this, round up to whole hours and warn: split the issue |
| `estimate.min_samples` | 5 | Samples needed before the history factor replaces the prior |
| `estimate.prior_factor` | 1.0 | Factor until then |
| `estimate.session_gap_minutes`, `estimate.session_lead_minutes` | 60, 15 | Git-proxy sessions |
