# Golden set — ground-truth labeling convention

**Decision:** the golden set is a small, hand-labeled, **append-only** ground-truth
corpus. It is the foundation the harness measures against (Rule 5 (Evals before
agents)). Quality and coverage of *failure categories* matter far more than size.

Follow every convention below when you add a doc.

## The labeling convention

| Rule of the road | What it means here |
|------------------|--------------------|
| **Labeling schema = the Pydantic model** | A label is a valid instance of the canonical record in [../../extract/schemas.py](../../extract/schemas.py). Don't invent a parallel label format — if the model can't hold a fact, fix the model. |
| **Stratify by visual layout** | Pick docs to span the *layout* space (single-column, two-column, scanned, table-heavy), not the topic space. Layout is what breaks extraction. |
| **One fact = one source quote** | Every labeled field carries the verbatim sentence it came from (Rule 3 (Quote the source)). If you can't quote it, it isn't a label. |
| **Saturation rule** | Stop labeling once ~20 traces in a row turn up **no new failure category** (Hamel Husain's saturation rule). ~50–100 traces/stage is plenty. |
| **Append-only** | Never delete or edit a label to make a number go up. The set only grows. |
| **Production failures promoted here forever** | Every real production failure (sampled from Langfuse traces) is promoted into the golden set permanently — an append-only asset that compounds weekly. |

## File layout

- `_sample_label.json` — one example labeled record (the leading `_` marks it as a
  sample/fixture, NOT real ground truth; the harness ignores `_`-prefixed files).
- Real labels: one JSON file per labeled doc, named after its source document.

## What a label is NOT

- Not a 1–5 quality score — use atomic pass/fail sub-checks (Rule 11 (Binary
  checks beat 1–5 scales)).
- Not a guess — if a field is ambiguous in the source, leave it null and note why;
  never label something you can't quote (Rule 2 (fail fast, don't guess)).

## Links

- Schema (the label format): [../../extract/schemas.py](../../extract/schemas.py)
