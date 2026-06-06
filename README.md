# NYC Civic Data Ingestion System

**A neighbor reads one email and knows what they need to know this week.**

NYC government publishes a flood of information about your neighborhood — community board agendas, land-use applications, building permits, housing-code violations, and Council hearings. Almost none of it reaches the residents it affects, because it's buried in dense PDFs and legal jargon scattered across dozens of agency websites.

This project pulls that data together, reads it, checks the facts against official NYC reference data so nothing is made up, and sends you a plain-English weekly (or daily) email about what's happening near your home: a hearing you can still testify at, a rezoning down the block, a permit on the building next door, or a hazardous violation that might signal pressure on tenants.

## Mission

Give residents a fighting chance to know — early, in plain English, and in time to act — what their own city is doing in their own neighborhood.

## Status

**Greenfield / Phase 0.** We are standing up the foundation: the project skeleton, the rules canon, and — first, before any pipeline is stable — the evaluation harness. Civic data's specific danger is being *confidently wrong* (a hallucinated hearing date is worse than silence), so the eval harness is built first, not last. No data is being ingested or delivered yet; the project is built in phases, each with a hard go/no-go gate.

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate                 # Windows (use `source .venv/bin/activate` on macOS/Linux)
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env                  # then fill in your keys; never commit .env
npm run eval                            # run the eval suite (promptfoo via package.json)
```

**Note:** Python 3.14 is the target, but it's new — some binary wheels (`psycopg`, `PyMuPDF`, `geosupport`) may lag. If a dependency fails to install, use Python 3.11–3.12. Geocoding uses NYC's official GeoSupport, which needs extra setup (binaries + env vars) documented in [.env.example](.env.example). The Phase-1 Legistar connector installs `python-legistar-scraper` from Git (it is not on PyPI) — see the note in [requirements.txt](requirements.txt).

## Architecture (short version)

The system is an assembly line of six stages — **Fetch → Parse → Extract → Normalize & validate → Store → Deliver** — with a permanent evaluation harness running alongside it. The core design choice is *many thin per-source connectors feeding one shared, city-agnostic core*: NYC-specific knowledge stays quarantined in clearly labeled modules, so the same machinery can serve a second city later by copy-and-swap.

New contributors should start with [CONTRIBUTING.md](CONTRIBUTING.md).
