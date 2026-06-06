# Contributing

Thanks for helping build this. The mission is narrow and serious: **give residents a fighting
chance to know — early, in plain English, and in time to act — what their city is doing in their
own neighborhood.** Because the output reaches real people about real buildings, the bar is not
"does it work" but "is it **correct, reproducible, and honest**." This project is run like applied
research that happens to ship email: every claim is sourced, and every result is reproducible.

## Core principles

A handful of non-negotiable principles shape every change:

- **Fail fast, don't guess.** Anything that doesn't validate is rejected or flagged into a
  quarantine table — never guessed into the database. A silent wrong answer is a lie told to a
  resident; a visible failure is a bug report.
- **Quote the source.** Every extracted fact carries the verbatim source sentence it came from, so
  any field traces back to the exact line in the original document. A change that drops provenance
  fails regardless of accuracy.
- **The NYC / agnostic seam.** NYC-specific knowledge (where agendas live, ULURP shapes, zoning
  lists, GeoSupport) lives **only** in `ingest/sources/nyc/` and clearly labeled lookup modules.
  The shared core (`parse/ extract/ normalize/ store/ deliver/ eval/`) **never mentions NYC**. If a
  change makes you edit the core to add a source, the seam is wrong — stop and reconsider.
- **LLM only on dirty inputs.** The extractor fires on PDF/HTML only; clean structured feeds
  (Socrata / Legistar JSON) skip parse and extract entirely. This is the biggest cost lever.
- **Evals gate changes.** The eval harness is the source of truth — "the eval is more important
  than the agent." A change to a prompt, the extractor, or a pipeline stage must clear evals before
  merge.
- **Config, not hard-codes.** Model names live behind config flags (`EXTRACT_MODEL`, `JUDGE_MODEL`),
  never hard-coded; secrets live in `.env` (git-ignored), with `.env.example` as the committed
  template.

## Dev setup

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env                  # fill in keys; NEVER commit .env
```

`make setup`, `make check` (lint + types + tests), and `make eval` wrap the common tasks.

## Before you open a PR

- [ ] **`make check` is green** — ruff, mypy, pytest all pass.
- [ ] **Evals pass if you touched a prompt, the extractor, or a pipeline stage** — with no
      block-deployment regression: extraction field-level **F1 must not drop >3pp**, **hallucination
      rate ≤1%** (every extracted value must be string-searchable in its source), geocoding
      median-error must not regress >20m, factual-consistency must not drop >5pp.
- [ ] **Every extracted fact still quotes its source**; model names stay behind config flags.
- [ ] **No NYC-specific code outside `ingest/sources/nyc/`** or a labeled lookup module.
- [ ] **New datasets or models are documented** — a short datasheet (what's in it, how it was
      collected and labeled, its license) or model card (intended use, eval results, known limits)
      lives beside the asset.
- [ ] **Shipped results carry a provenance/run manifest** — enough to reproduce them (source-data
      version, code commit, model + prompt version, fetch timestamps). A result you can't reproduce
      isn't done.
- [ ] **Resident-linked or displacement changes got a privacy & harm review** — name buildings, not
      people, unless necessary; assert sourced facts, not intent; deliver to the affected resident,
      not their adversary; keep PII minimal.
- [ ] **No secrets committed.** Update `.env.example` for new flags only.

## Commits & PRs

- **Concise, feature-based commit messages** — one logical change per commit, present-tense subject
  line.
- **One logical change per PR.** Fill in the PR template; link any issue.
- **Sign off your commits (DCO).** Add a `Signed-off-by` line with `git commit -s` to certify you
  have the right to contribute the change under the project license (the
  [Developer Certificate of Origin](https://developercertificate.org/)).
- PRs run CI (lint + types + tests) and the promptfoo eval check. Green before review.

## Reporting issues

- **Bugs / features** → the GitHub issue templates.
- **A wrong or misleading civic fact** (a bad hearing date, a questionable displacement flag) → the
  **data-quality** issue template. These are the highest-priority issues this project has — being
  *confidently wrong* is its worst failure mode.
- **A security or privacy vulnerability** → do **not** open a public issue; follow
  [SECURITY.md](SECURITY.md).

By contributing you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
