"""NYC Civic Data Ingestion System — the `ingest` package.

A six-stage assembly line that turns messy NYC government documents into a
plain-English neighborhood email digest. The line is mostly deterministic
Python; LLMs live only where deterministic code is too brittle (PDF extraction,
vision navigation, fuzzy entity resolution).

The six stages
--------------
1. Fetch     — knows where documents live; pulls down new ones. (no LLM)
2. Parse     — PDF/HTML -> uniform ``{text, page_images, layout}``. Structured
               JSON skips this stage. (vision-LLM only on scanned/messy pages)
3. Extract   — parsed content + strict schema -> JSON facts; every fact quotes
               its source sentence. (LLM, dirty inputs only)
4. Normalize & validate — address -> BBL, zoning vs canonical list, ULURP
               format + ZAP existence; fail fast -> quarantine. (no LLM)
5. Store     — verified record -> Postgres+PostGIS with source quotes. (no LLM)
6. Deliver   — match records to subscribers by location; rank; human-review
               top-N; send plain-English digest. (no LLM at send time)

Eval sits *beside* the line, not inside it — a permanent harness measuring each
stage against hand-labeled ground truth (built first, not last).

The boundary (the one design decision)
--------------------------------------
NYC-specific code lives ONLY under ``ingest/sources/nyc/`` and labeled lookup
modules; the city-agnostic core (Parse, Extract machinery, Eval, Store, Deliver)
never mentions NYC. To add a second city: copy the connector, swap the lookup,
reuse the core untouched.

See also
--------
- Project map / setup / stack: [../CLAUDE.md](../CLAUDE.md)
- Architecture, clean seams, the boundary: [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
"""

from __future__ import annotations

__all__: list[str] = []

# TODO Phase 0: expose a stable top-level surface (e.g. version, stage entrypoints)
# once the core loop is proven. Keep this re-export list minimal until then.
