<!-- Thanks for contributing. Keep one logical change per PR. See CONTRIBUTING.md. -->

## What & why

<!-- One paragraph: what this changes and why. Link the issue it closes (Closes #N). -->

## Which stage does this touch?

<!-- e.g. Fetch (sources/), Parse, Extract, Normalize, Store, Deliver, or the eval harness. -->

## Checklist

- [ ] `make check` is green (ruff + mypy + pytest).
- [ ] I did **not** add NYC-specific code outside `ingest/sources/nyc/` or a labeled lookup module.
- [ ] If I touched a **prompt / extractor / pipeline stage**, evals pass with no block-deployment regression (extraction F1 not down >3pp; hallucination ≤1%; geocoding median not >20m worse; factual-consistency not >5pp worse).
- [ ] Every extracted fact still **quotes its source**; model names stay behind config flags.
- [ ] If I added/changed a **dataset or model**, I updated its datasheet / model card.
- [ ] If this **ships a result/digest**, it carries a provenance/run manifest so the result is reproducible.
- [ ] If this touches **resident-linked or displacement output**, it got a privacy & harm review.
- [ ] No secrets committed; `.env.example` updated for any new flags.
- [ ] Commits are signed off (`git commit -s`, DCO).

## How I verified this

<!-- Tests added/run, eval diff, manual check. For data changes, paste the source link so a reviewer can verify against the city's own record. -->
