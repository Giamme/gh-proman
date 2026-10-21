# Comments and the status block

gh-proman writes to issues in exactly two ways, and nothing else in an issue is ever touched.

## 1. One comment per event

Every comment starts with a hidden marker, followed by your text and a footer:

```
<!-- gh-proman:v1 event=pr-opened source=collitude/ctrlrisk-tools-redigo#40 sha=4997733e3e49 -->
<your text>

<sub>gh-proman · aggiornamento automatico (Claude Code) · 21/10/2026</sub>
```

- The marker key is `(event, source[, key])`. Running `comment` again with the same key updates your earlier comment in place, or does nothing if the text is the same. Re-running a sync therefore never piles up duplicates.
- Only your own comments (the authenticated user's) are ever edited. A teammate's agent syncing the same PR writes its own comment.
- The `sha` (or the audited head) becomes the issue's watermark for the next audit.
- Markers inside your text are neutralised, so you can't break the bookkeeping by quoting one.

| Event | When | Source |
|---|---|---|
| `issue-related` | a new issue/PR changes something for this issue | the new item, `owner/repo#N` |
| `duplicate-candidate` | two issues look like the same work and you can't close either | the other issue |
| `pr-opened` | a PR that works on this issue opened | the PR |
| `pr-merged` | it merged into an integration branch but doesn't complete the issue | the PR |
| `pr-merged-partial` | a stacked PR merged into another feature branch | the PR |
| `closed` | written by `close` itself | the evidence |
| `audit` | an audit found changes the issue should know about | `owner/repo@branch`, with `--sha <head>` |
| `note` | anything else worth keeping with the issue | whatever caused it |

### What to write

- Three to six lines, in the config language.
- Say what changed **for this issue**, where (the link), what remains, and anything the next agent must know: decisions, caveats, a flag that must be turned on.
- No greetings, no restating the issue, no file-by-file diff, no test counts unless they matter for acceptance.

Italian, PR opened:

```
La PR collitude/ctrlrisk-tools-redigo#40 unifica il calcolo della scadenza tra catalogo e matching (resolver comune, proroga valida prevalente).
Resta da allineare il consumer Dashboard (S03 lato Dashboard) e da eseguire T03 sul candidato.
Nota: la fine giornata resta UTC; il cambio di fuso non fa parte di questa PR.
```

English, related issue:

```
#58 (CSV export) adds a shared `formatItalianNumber` helper in `src/lib/format.ts`; this issue's PDF totals can reuse it instead of the local formatter.
Nothing is decided here yet.
```

Italian, merged but not done:

```
collitude/ctrlrisk-intelligence#61 è in dev: il pulsante Esporta CSV e i filtri sono fatti.
Restano formato date italiano e BOM per Excel (criteri 2 e 3).
```

## 2. The status block

One block per issue, between `<!-- gh-proman:status:start -->` and `<!-- gh-proman:status:end -->`, at the top of the body (configurable). It's the consolidated current state, so an agent reads one place instead of the whole thread:

```
<!-- gh-proman:status:start -->
> **Stato (gh-proman)** · In review · aggiornato 21/10/2026
> Resolver comune pronto; manca il consumer Dashboard.
> PR: collitude/ctrlrisk-tools-redigo#40 (draft → dev-deb), collitude/ctrlrisk-dashboard#28 (aperta → dev-deb)
> Criteri coperti: 1/3
> Ultimo controllo del codice: collitude/ctrlrisk-tools-redigo@4997733
<!-- gh-proman:state {"covered":{…},"prs":[…],"status":"in_review","summary":[…],"v":1} -->
<!-- gh-proman:status:end -->
```

- The script renders it from structured state (the hidden `gh-proman:state` JSON). You only supply the `--summary` lines (one or two), `--status`, `--pr`, `--criteria` and `--covered`. Unspecified parts are kept.
- **Safety.** Every byte outside the markers is preserved, which the script verifies after writing. If the markers are duplicated or unbalanced, it refuses and you post a comment instead. If someone edits the body at the same moment, it recomputes from the fresh copy. It also refuses past GitHub's 65,536-character limit.
- Track every PR that belongs to the issue, including the other repo's half of a coordinated change. `close` refuses while a tracked PR hasn't landed.
