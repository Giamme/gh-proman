# Configuration

gh-proman acts only where a config applies. Without one, the CLI stops with a hint and the hook stays silent, so installing the plugin never touches repositories that haven't opted in.

## Resolution order

1. `<repo>/.github/gh-proman.json`: committed, so the whole team shares it.
2. `~/.config/gh-proman/config.json` (or `$GH_PROMAN_CONFIG`, or `$XDG_CONFIG_HOME/gh-proman/config.json`). It holds profiles, and the first profile whose `match` globs fit the repo's `owner/repo` wins.
3. Nothing: gh-proman is off for that repo.

When both exist, the repo file is merged over the matched profile, key by key. `GH_PROMAN_DISABLE=1` turns everything off, the hook included.

`config show --json` prints the merged result and the files it came from.

## Keys

| Key | Default | Meaning |
|---|---|---|
| `project.owner`, `project.number` | required | The board: `https://github.com/orgs/<owner>/projects/<number>`, or `users/<owner>/…` |
| `language` | `it` | Language of issue bodies, comments and the status block (`it`, `en`) |
| `integration_branches` | `["@default"]` | Branches whose merges count as done. `@default` is the repo's default branch. A PR merged elsewhere (a stacked base) counts only once its merge commit is reachable from one of these |
| `status` | Backlog / Ready / In progress / In review / Done | Board option names for the keys `backlog`, `ready`, `in_progress`, `in_review`, `done` |
| `status_field` | `Status` | Name of the Status field |
| `pr_status`, `draft_pr_status` | `in_review`, `in_progress` | Status set on linked issues when a PR (or draft PR) opens |
| `priority.field`, `priority.options`, `priority.default` | `Priority`, P0–P3, `P2` | The Priority field, and the levels agents use |
| `priority.source` | `auto` | `auto`: the organisation's issue field named `priority.field` if there is one, else the board's project field. `issue` / `project` force one |
| `priority.map` | `{}` | Level → option name, e.g. `{"P0": "Urgent"}`. Without it, an option named like the level is used, else the standard names by position (Urgent/Critical, High, Medium/Normal, Low) |
| `issue_types` | bug→Bug, feature→Feature, chore→Task | Organisation issue type set by `create`; skipped with a warning if the org has no such type |
| `title_format` | `[{id} · {priority}] {title}` | Title when `create --id` is given |
| `plain_title_format` | `[{priority}] {title}` | Title without an ID. A title that already starts with `[` is kept as is; `""` turns the prefix off |
| `task_id_pattern` | `\b[A-Z]\d{2}(?:\.\d+)?\b` | Task IDs (`S03`, `T01.2`) recognised in titles, branches and commits |
| `status_block_position` | `top` | Where a new status block goes in an issue body (`top` / `bottom`) |
| `duplicate_threshold` | `0.5` | Score at which `create` stops and shows possible duplicates |
| `autoclose.enabled` | `true` | `false`: `close` always refuses; agents comment instead |
| `autoclose.max_closes_per_run`, `autoclose.max_actions_per_run` | 3, 20 | Per process (`apply` plan or single command) |
| `related_owners` | `[]` | Reserved for searching issues outside the board |
| `estimate.field` | `Estimate` | Board Number field for hands-on hours; `project setup` creates it if missing |
| `estimate.scale`, `estimate.max_hours` | 0.5…13 h, 13 | Values estimates snap to; above `max_hours` the result warns to split the issue |
| `estimate.min_samples`, `estimate.prior_factor` | 5, 1.0 | Closed issues with estimate and actual needed before calibration replaces the prior factor |
| `estimate.session_gap_minutes`, `estimate.session_lead_minutes` | 60, 15 | How commit times become hands-on hours for calibration. `references/estimates.md` has the method |

## Examples

Team board, shared by every repo of the org (user profile):

```json
{
  "profiles": [
    {
      "match": ["collitude/*"],
      "language": "it",
      "project": { "owner": "collitude", "number": 1 },
      "integration_branches": ["dev", "dev-deb", "@default"]
    }
  ]
}
```

A repo that tracks its work on an external client's board, in English:

```json
{
  "version": 1,
  "language": "en",
  "project": { "owner": "client-org", "number": 4 },
  "integration_branches": ["main"],
  "status": { "in_review": "Review", "done": "Shipped" }
}
```

Integration branches that don't exist in a given repo are ignored there. `doctor` lists them as "not in this repo".

## Priority: board field or organisation issue field

Organisations can define **issue fields** (Settings → Issues → Fields), such as a Priority with Urgent/High/Medium/Low. A board then shows that field as a read-only mirror: it can't be edited as a project field, and `project setup` leaves it alone. gh-proman detects this and writes the priority on the issue itself, mapping P0–P3 to the org's options; `doctor` prints the mapping. Agents keep saying P0–P3, and titles keep the `[S03 · P0]` prefix.

When the board has its own Priority field instead (or none), `project setup` adds the options P0–P3 to it, keeping any existing options' ids so items keep their values.

## Auth

gh-proman uses the GraphQL API, so the token needs the `repo`, `project` and `read:org` scopes. Without `read:org`, GitHub silently hides the organisation's issue fields and refuses its issue types. `doctor` checks all three. Two pitfalls:
- An exported `GITHUB_TOKEN` or `GH_TOKEN` overrides `gh`'s keyring login. When it is stale, every call fails with "Bad credentials", and `gh auth refresh` doesn't help: unset or replace the variable.
- Fine-grained tokens need "Projects: read and write" on the organisation, plus "Issues" and "Pull requests" read/write on each repo.
