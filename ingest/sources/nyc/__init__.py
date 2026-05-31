"""Stage 1 (Fetch) — NYC connectors. NYC-SPECIFIC: every NYC fact lives here.

Single responsibility: the NYC city subpackage — the only place in the codebase
allowed to know NYC specifics (CB agenda URLs, ULURP number shape, ZAP/Legistar/
Socrata endpoints, DOB/HPD dataset ids). This is the wild side of the boundary:
discovery + extraction per source.

The five connectors
-------------------
- ``cb_agenda``   — community board agendas (DIRTY/PDF) -> Parse -> Extract.
- ``ulurp_packet``— land-use review packets (DIRTY/PDF, hundreds of pages).
- ``zap_api``     — NYC ZAP land-use feed (STRUCTURED, snapshot only; no LLM).
- ``legistar``    — Council hearings / LU Committee / roll-call (STRUCTURED; no LLM).
- ``dob_hpd``     — DOB NOW permits + HPD violations via Socrata (STRUCTURED; no LLM).

Rules honored
-------------
- Rule 4 (NYC-specific code in nyc/): this package is the *only* legal home for it.
- Rule 1 (LLM only on dirty inputs): structured connectors above never call an LLM.

See per-package contract in [CLAUDE.md](CLAUDE.md), the canon in
[../../../docs/RULES.md](../../../docs/RULES.md), and root [../../../CLAUDE.md](../../../CLAUDE.md).
"""

from __future__ import annotations

__all__: list[str] = []

# TODO Phase 1: structured connectors (dob_hpd, legistar) land first.
# TODO Phase 2: dirty connectors (cb_agenda, then ulurp_packet) once eval is proven.
