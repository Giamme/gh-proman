# gh-proman

GitHub Projects issue management for teams whose agents (Claude Code, Codex, …) all work on the same board.

When every developer runs their own agent, each agent knows only what it did itself. gh-proman makes the GitHub issues the shared memory:

- **Create**: well-formed issues from per-type templates (bug, feature, chore; Italian or English), added to the configured board with Status and Priority. A **duplicate guard** stops and shows similar issues first.
- **Sync, automatically**: a hook fires after an agent runs `gh issue create`, `gh pr create` or `gh pr merge`, or the GitHub MCP equivalents, and asks it to sync the related issues. Those issues get:
  - a short context comment;
  - a status block in the issue body with the consolidated state;
  - a Status move on the board;
  - an **auto-close** when a merged PR completes them, at high confidence only.
- **Audit**: reconciles a repository's history with its open board issues. It finds what's already done, what needs an update, and PRs merged in the web UI that no agent synced.

The agent does the judging; a stdlib Python CLI (`skills/gh-proman/scripts/gh_proman.py`) does all GitHub and git I/O. Writes are idempotent (hidden markers), guarded, and available as `--dry-run`.

## Install

### Claude Code

```
/plugin marketplace add Giamme/gh-proman
/plugin install gh-proman@gh-proman
```

From a shell: `claude plugin marketplace add Giamme/gh-proman && claude plugin install gh-proman@gh-proman`. The plugin brings the skill and the sync hook.

### Codex

Codex reads the same plugin, hook included:

```sh
codex plugin marketplace add Giamme/gh-proman
codex plugin add gh-proman@gh-proman
```

Codex asks you to review and trust the hook before it runs.

### From a clone (any agent)

To track `main` and update every agent with a `git pull`:

```sh
git clone https://github.com/Giamme/gh-proman ~/src/gh-proman
~/src/gh-proman/scripts/install --dry-run   # see what would be linked
~/src/gh-proman/scripts/install             # every detected agent; or --client codex --client agents
~/src/gh-proman/scripts/install --uninstall
```

The installer:
- symlinks the skill into each agent's skill directory;
- links `~/.local/bin/gh-proman`;
- for Codex, adds the hook to `~/.codex/hooks.json` (`--no-hooks` to skip).

Install the skill in one place per agent: with the Claude Code plugin installed, don't also link it into `~/.claude/skills`.

### Requirements

- Python 3.9+
- `gh` 2.48+, authenticated with the `repo`, `project` and `read:org` scopes (`gh auth refresh -s project,read:org`)

Watch out for a stale exported `GITHUB_TOKEN`: it overrides the keyring login. `gh-proman doctor` tells you which token is in use.

## Configure

gh-proman does nothing in a repository until a config applies, and the hook stays silent there.

- **For the whole team:** commit `.github/gh-proman.json` in each repo.
- **For yourself:** add a profile to `~/.config/gh-proman/config.json`, matched by `owner/*`.

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

Any repo can point at another board (an external client's project) with its own `.github/gh-proman.json`. All keys are in `skills/gh-proman/references/config.md`.

Then ask your agent to "set up gh-proman": it runs `doctor` and `project setup`.

- If your organisation defines Priority as an issue field (Urgent/High/…), gh-proman maps P0–P3 onto it.
- Otherwise it adds P0–P3 to the board, after you confirm.

Issues also get the org's issue type (Bug, Feature, Task) when one exists.

## Use

Talk to your agent:

- "Apri una issue per il login Google che resta in attesa, P1": template, lint, duplicate guard, create on the board.
- `gh pr create …` / `gh pr merge …` as usual: the hook makes the agent sync right after.
- "Audit the board against this repo" / "aggiorna le issue": audit, a summary, then the changes after you confirm closes.

The CLI is usable directly too: `gh-proman doctor`, `gh-proman candidates --pr <url>`, `gh-proman sync --source <url> --dry-run`, `gh-proman audit-scan`. Every command has `--help`.

## What it writes, and what it never touches

- **Comments**: one per event, each carrying a hidden marker such as `<!-- gh-proman:v1 event=pr-opened source=owner/repo#40 -->`. Re-runs update the same comment instead of adding new ones.
- **Status block**: one block per issue body, between `<!-- gh-proman:status:start -->` and `<!-- gh-proman:status:end -->`. Everything outside it is preserved byte for byte, which is verified after each write.
- **Board fields**: Status (forward only) and Priority.
- **Closes**: only when the PR has a closing link to the issue (Development sidebar, `Closes`, or the task ID in the title/branch). The work must have landed in an integration branch, every open checkbox must be mapped to evidence, and every tracked PR must have landed. The closing comment comes first and says how to undo. The rubric is in `skills/gh-proman/references/rubric.md`.

It never edits other people's comments or text in issue bodies, never ticks their checkboxes, and never closes because a PR was merely opened.

## Develop

```sh
python3 -m unittest discover -s tests -v
```

The tests run against a stateful fake `gh` (`tests/fakes/gh`) and throwaway git repos; nothing reaches GitHub. `skills/gh-proman/evals/` holds trigger and behaviour evals; `evals/fixtures/make_fixture.py <scenario> <dir>` builds a sandbox with the fake `gh`.

## License

MIT
