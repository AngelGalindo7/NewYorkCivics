"""Stage 1 (Fetch) — per-source connectors: the *wild* side of the boundary.

Single responsibility: hold the many thin, per-source connectors that know where
documents live and how to pull down new ones. Each connector handles discovery +
fetch for exactly one source; the shared core (Parse, Extract, Normalize, Store,
Deliver) handles everything on the clean side.

Boundary
--------
This package is a NAMESPACE for connectors, not city-agnostic machinery itself.
All city-specific knowledge (where CB agendas live, what a ULURP number looks
like, how to call GeoSupport) is quarantined into city subpackages — currently
``ingest/sources/nyc/`` only. See Rule 4 (NYC-specific code in nyc/).

Adding a city = add a sibling subpackage (e.g. ``sources/jersey_city/``), copy
the connector shape, swap the lookup module. The core never changes (Rule 14,
port a city by copy-and-swap).

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): structured connectors emit clean records
  with no LLM; only PDF/HTML connectors hand off to Parse -> Extract.
- Rule 4 (NYC-specific code in nyc/): no NYC names appear at this level.

"""

from __future__ import annotations

__all__: list[str] = []

# TODO Phase 4: when porting a second city, add its subpackage here and confirm
# nothing in the shared core needs to change (the seam test).
