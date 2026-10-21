# Writing issues

An issue on the board is read by people and by every agent that picks it up later, often with no other context. Write it so that someone who wasn't in your conversation can start, and can tell when it's done.

## Templates

`assets/templates/<language>/<type>.md`:

| Type | Use for | Sections |
|---|---|---|
| `bug` | something that worked, or should work, and doesn't | Contesto, Come riprodurre (Atteso/Ottenuto), Attività, Criteri di chiusura, Test e verifica, Riferimenti |
| `feature` | new or changed behaviour | Contesto, Obiettivo, Fuori perimetro (optional), Attività, Criteri di accettazione, Test e verifica, Riferimenti |
| `chore` | internal work: CI, dependencies, refactors, data fixes | Contesto, Attività, Verifica, Riferimenti |

English templates have the same sections (Context, Steps to reproduce, Tasks, Closing criteria / Acceptance criteria, Tests and verification, References).

- Keep every heading; `lint-body` warns when one is missing. Drop the `<!-- hints -->`.
- If a section doesn't apply, write one line saying so ("Nessuna migrazione.").

## Checklists

Checklists are how agents and people know what's left, and how gh-proman decides whether an issue can close. Every unchecked box must be backed by evidence before an automatic close.

- **Attività / Tasks**: the steps, each small enough to finish in one sitting. 2–6 items.
- **Criteri / Acceptance criteria**: observable outcomes, not steps. Each one can be checked by looking at the product or a test. "Il login con Google completa in meno di 3 s", not "Sistemare il login".
- **Test e verifica**: what proves it. Name the kind of test (unit, browser, regression) and where it's checked (locale, dev, stage).

Don't pad: three sharp criteria beat ten vague ones. Don't tick boxes in someone else's issue. Report progress in a comment or the status block.

## Titles

- Every title starts with a bracketed prefix, like the board's existing tasks. Pass the bare title; `create` adds the prefix, and `set-priority` keeps it in sync with the Priority field.
- Plain issues: an imperative or a symptom, under about 70 characters. `create --priority P1` produces `[P1] Login con Google resta in attesa dopo il consenso`. Don't repeat the priority in the title text.
- Planned tasks with an ID: `create --id S03 --priority P0` produces `[S03 · P0] <title>`.
- Bugs say where: "Drawer Fascicolo: le schede non si ripristinano".

## References

- Code: `permalink path:line` → `https://github.com/<o>/<r>/blob/<sha>/path#L12`. SHA links don't rot; branch links are rejected by `lint-body`.
- Issues and PRs: `Refs owner/repo#N`. Cross-repo references are normal on this board (Dashboard ↔ Redigo ↔ RNA).
- Evidence (logs, screenshots, QA reports): link them. Never paste secrets, tokens or customer data; `lint-body` blocks obvious tokens.

## House style seen on the board

The board's planned tasks (e.g. `[S03 · P0] Scadenza effettiva coerente tra matching, catalogo e pratica`) use:
- a short field table (Priorità, Tipo, Responsabilità, Dipendenze);
- "Prima di iniziare" (check what's already done in other branches first);
- "Capacità da riutilizzare", "Residuo / attività", "Criteri di chiusura e prove";
- permalinks at SHA;
- a "Ricevuta di chiusura".

Their authors record progress as dated sections between their own HTML markers (`<!-- s03-execution-2026-10-20:start -->`).

When you work on those issues:
- never edit their sections or markers;
- keep updates in your comments and the gh-proman status block;
- respect their closing receipt: if the issue asks for a closing receipt or acceptance (collaudo) that your evidence doesn't cover, comment and leave it open.

For everyday bugs and features, the lighter templates are enough. Use the field table only when it adds information (priority is already a board field).

## Example (feature, Italian)

```markdown
## Contesto
Gli analisti esportano i report a mano copiando le tabelle; serve un export diretto.

## Obiettivo
Dal drawer Report si scarica un CSV con le stesse colonne e filtri della tabella visibile.

## Fuori perimetro
Export Excel e PDF.

## Attività
- [ ] Server Action che genera il CSV dai filtri correnti
- [ ] Pulsante "Esporta CSV" nel drawer Report
- [ ] Aiuto contestuale e pagina docs IT/EN aggiornati

## Criteri di accettazione
- [ ] Il CSV contiene le colonne visibili, nell'ordine della tabella, con i filtri applicati
- [ ] Numeri e date in formato italiano; UTF-8 con BOM per Excel

## Test e verifica
- [ ] Unit test della generazione (filtri, caratteri accentati, 0 righe)
- [ ] Verifica su dev con un report reale

## Riferimenti
- Tabella attuale: https://github.com/collitude/ctrlrisk-intelligence/blob/<sha>/src/components/reports/table.tsx#L40
- Refs collitude/ctrlrisk-intelligence#12
```
