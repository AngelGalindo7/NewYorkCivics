# Extraction prompts — versioning convention

**Decision: prompts are versioned files, never edited in place.** Each prompt
lives at `ingest/extract/prompts/<doc>.vN.md`. To change a prompt, copy it to
`<doc>.v(N+1).md` and edit the copy. This is the same discipline as keeping the
model name behind a config flag, extended to prompts: the *artifact* that drives a
model is itself swappable and auditable, so a regression is a diff between two
files, not lost history.

## The convention

| Rule | Why |
|------|-----|
| Filename = `<doc>.vN.md` | `<doc>` matches the document type (e.g. `cb_agenda`); `N` is a monotonic integer starting at `v1`. |
| Never edit a shipped version | Once a `vN` has run in CI/production, it is frozen. Iterate by adding `v(N+1)`. |
| Eval diffs prompt versions | A change is accepted only when the eval shows F1 not down >3pp and hallucination ≤1% (the gate). The promptfoo before/after diff is the canary. |
| One doc type per file family | `cb_agenda.vN.md`, `ulurp_packet.vN.md`, etc. — don't mix doc types in one prompt. |

## How to iterate (the workflow)

1. Copy `<doc>.vN.md` -> `<doc>.v(N+1).md`; edit the new file only.
2. PR-shadow: replay the last 50 production traces through old + new prompts.
3. Run the promptfoo before/after diff against the eval thresholds (F1 ≥0.80;
   block on >3pp regression; hallucination ≤1%).
4. Block the change on any regression; otherwise the new version becomes the
   default the connector points at.
