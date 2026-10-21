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
| `priority.field`, `priority.options`, `priority.default` | `Priority`, P0–P3, `P2` | The Priority single-select field |
| `title_format` | `[{id} · {priority}] {title}` | Title when `create --id` is given; without an ID the title is used as is |
| `task_id_pattern` | `\b[A-Z]\d{2}(?:\.\d+)?\b` | Task IDs (`S03`, `T01.2`) recognised in titles, branches and commits |
| `status_block_position` | `top` | Where a new status block goes in an issue body (`top` / `bottom`) |
| `duplicate_threshold` | `0.5` | Score at which `create` stops and shows possible duplicates |
| `autoclose.enabled` | `true` | `false`: `close` always refuses; agents comment instead |
| `autoclose.max_closes_per_run`, `autoclose.max_actions_per_run` | 3, 20 | Per process (`apply` plan or single command) |
| `related_owners` | `[]` | Reserved for searching issues outside the board |

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

## Auth

gh-proman uses the GraphQL API for boards, so the token needs the `repo` and `project` scopes (`read:org` is not required). Two pitfalls:
- An exported `GITHUB_TOKEN` or `GH_TOKEN` overrides `gh`'s keyring login. When it is stale, every call fails with "Bad credentials", and `gh auth refresh` doesn't help: unset or replace the variable.
- Fine-grained tokens need "Projects: read and write" on the organisation, plus "Issues" and "Pull requests" read/write on each repo.
