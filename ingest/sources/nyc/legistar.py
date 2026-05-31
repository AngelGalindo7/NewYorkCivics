"""Stage 1 (Fetch) — NYC Council / Legistar. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull NYC City Council activity from Legistar — hearings,
Land Use Committee items, and roll-call votes — as clean structured data and map
it to the canonical event shape. Legistar is a structured public API, so this
connector emits records directly and SKIPS Parse and Extract entirely.

Resident value: "what hearing can I still testify at" and "how did my Council
Member vote" come straight from this source with no extraction work.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): structured -> NO LLM, ever. This is the
  single biggest cost lever — clean JSON never touches the extractor.
- Rule 4 (NYC-specific code in nyc/): Legistar client + NYC field mapping are NYC.
- Rule 15 (SoR key = (source_id, source_record_id) + BBL): per-record identity.

Built on python-legistar-scraper (the proven Council ingestion pattern); study
datamade/nyc-councilmatic and opencivicdata/scrapers-us-municipal for the shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ingest.extract.schemas import CivicEvent

SOURCE_ID = "nyc_legistar"


def discover_events(since: str | None = None) -> Iterator[CivicEvent]:
    """Stream Council hearings / LU Committee items / roll-call votes.

    Args:
        since: Optional ISO timestamp; yield only items changed at/after it.
            ``None`` does a full pull (used for backfill).

    Yields:
        One :class:`~ingest.extract.schemas.CivicEvent` per Legistar item,
        with ``source_id == SOURCE_ID`` and the Legistar matter/event id as
        ``source_record_id`` (Rule 15). No LLM (Rule 1).
    """
    raise NotImplementedError(
        "Phase 1: pull Legistar hearings/LU items/roll-calls via python-legistar-scraper."
    )


def fetch_roll_call(matter_id: str) -> CivicEvent:
    """Fetch the roll-call vote breakdown for one matter.

    Args:
        matter_id: Legistar matter id whose per-member votes are wanted.

    Returns:
        A canonical event carrying the roll-call (who voted how) in ``extras``.
    """
    raise NotImplementedError("Phase 1: fetch per-member roll-call for a matter.")


# TODO Phase 1: declarative pull config (base url, paging) + incremental `since`.
# TODO Phase 1: thread Council items to the same project_thread_id as the related
#   ZAP/CB-agenda land-use story (Rule 7).
