"""Stage 1 (Fetch) — NYC ZAP land-use feed. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull NYC ZAP (Zoning Application Portal) land-use
application records as clean structured data and map them to the canonical event
shape. ZAP records are structured, so this connector emits records directly and
SKIPS Parse and Extract entirely.

SNAPSHOT ONLY
-------------
The ZAP feed is exposed as a *snapshot*, not an incremental stream — there is no
reliable ``:updated_at`` cursor to page on. Each run re-pulls the current set and
dedups against the store on ``(source_id, source_record_id)`` (Rule 15). This is
unlike Socrata sources (see ``dob_hpd``), which DO support incremental cursors.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): structured -> NO LLM, ever.
- Rule 4 (NYC-specific code in nyc/): ZAP endpoints + field mapping are NYC.
- Rule 15 (SoR key = (source_id, source_record_id) + BBL): snapshot dedup key.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Canonical clean-record model from the city-agnostic Extract stage.
    from ingest.extract.schemas import CivicEvent

SOURCE_ID = "nyc_zap"


def fetch_snapshot() -> bytes:
    """Pull the current ZAP feed as a single snapshot payload.

    Returns:
        Raw feed bytes (the full current set; ZAP is snapshot-only, not incremental).
    """
    raise NotImplementedError("Phase 2: pull ZAP snapshot feed (no incremental cursor available).")


def iter_records(snapshot: bytes) -> Iterator[CivicEvent]:
    """Map a ZAP snapshot to canonical events.

    Args:
        snapshot: Raw bytes from :func:`fetch_snapshot`.

    Yields:
        One :class:`~ingest.extract.schemas.CivicEvent` per ZAP application,
        with ``source_id == SOURCE_ID`` and the ZAP application id as
        ``source_record_id`` (Rule 15). No LLM (Rule 1).
    """
    raise NotImplementedError(
        "Phase 2: map ZAP fields -> CivicEvent; dedup on (source_id, source_record_id)."
    )


# TODO Phase 2 / v2: ZAP is a Phase 2 / v2 source per source order — confirm scope.
# TODO Phase 2: thread ZAP filings to the same project_thread_id as related CB
#   agenda / Council items (Rule 7).
