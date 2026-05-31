"""Stage 1 (Fetch) — DOB NOW permits + HPD violations via Socrata. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull NYC DOB NOW building permits and HPD code violations
from NYC Open Data (Socrata / SODA) as clean structured data, map them to the
canonical event shape, and expose the cross-feed displacement signal. Both feeds
are structured JSON, so this connector emits records directly and SKIPS Parse and
Extract entirely.

Resident value: "what's on my building" (HPD violations) and "what's being built
near me" (DOB permits, filter NB / A1 / DM). These are the Phase 1 first sources.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): structured -> NO LLM, ever.
- Rule 4 (NYC-specific code in nyc/): dataset ids, cursor field, displacement
  thresholds are NYC knowledge and stay here.
- Rule 15 (SoR key = (source_id, source_record_id) + BBL): per-record identity;
  BBL is the join key the displacement signal correlates on.

Declarative feed config (dlt-style): each feed is a `SocrataFeed` describing
``base_url``, the ``paginator`` strategy, the ``incremental`` cursor on
``:updated_at``, and the ``primary_key`` — so a new Socrata feed is a config
entry, not new fetch code. A free Socrata app token gives ~1000 req/hr.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ingest.extract.schemas import CivicEvent

SOURCE_ID_DOB = "nyc_dob_now"
SOURCE_ID_HPD = "nyc_hpd_violations"


@dataclass(frozen=True)
class SocrataFeed:
    """Declarative description of one Socrata dataset pull (dlt-style).

    Attributes:
        source_id: Stable connector id used in the SoR key (Rule 15).
        base_url: Socrata SODA endpoint for the dataset (NYC Open Data).
        primary_key: Field(s) uniquely identifying a record -> ``source_record_id``.
        incremental_cursor: Field to page incrementally on (default ``:updated_at``).
        paginator: Paging strategy name (e.g. ``"offset"``); kept declarative.
    """

    source_id: str
    base_url: str
    primary_key: tuple[str, ...]
    incremental_cursor: str = ":updated_at"
    paginator: str = "offset"


# Declarative feed registry. TODO Phase 1: fill dataset ids + primary keys from
# NYC Open Data (DOB NOW permit issuance; HPD housing maintenance violations).
DOB_PERMITS_FEED = SocrataFeed(
    source_id=SOURCE_ID_DOB,
    base_url="",  # TODO Phase 1: DOB NOW permit-issuance SODA endpoint
    primary_key=(),  # TODO Phase 1: e.g. ("job_filing_number",)
)
HPD_VIOLATIONS_FEED = SocrataFeed(
    source_id=SOURCE_ID_HPD,
    base_url="",  # TODO Phase 1: HPD violations SODA endpoint
    primary_key=(),  # TODO Phase 1: e.g. ("violationid",)
)


def iter_feed(feed: SocrataFeed, since: str | None = None) -> Iterator[CivicEvent]:
    """Pull one Socrata feed incrementally and yield canonical events.

    Args:
        feed: The declarative feed config to pull.
        since: Optional cursor value; yield only records with
            ``feed.incremental_cursor`` at/after it. ``None`` does a full backfill.

    Yields:
        One :class:`~ingest.extract.schemas.CivicEvent` per Socrata record,
        keyed by ``(feed.source_id, primary_key)`` (Rule 15). No LLM (Rule 1).
    """
    raise NotImplementedError(
        "Phase 1: paginate Socrata feed; incremental cursor on :updated_at; map -> CivicEvent."
    )


def discover_displacement_signals(since: str | None = None) -> Iterator[CivicEvent]:
    """Emit buildings flagged by the displacement signal (cross-feed correlation).

    Signal definition (NYC-SPECIFIC, tunable): a Class C HPD violation in the last
    90 days AND a permit (especially Alt-1 / DM) in the last 180 days on the SAME
    BBL. Correlates the DOB and HPD feeds on BBL (Rule 15).

    Args:
        since: Optional cursor to bound the correlation window's freshness.

    Yields:
        One canonical event per flagged building, with the contributing HPD
        violation + permit quoted in ``extras`` for review.

    Note:
        Do NOT ship until a tenant organizer validates ~20 flagged buildings as
        plausible (Phase 1 sanity check / pivot threshold).
    """
    raise NotImplementedError(
        "Phase 1: correlate Class C HPD (90d) + Alt-1/DM permit (180d) on same BBL."
    )


# TODO Phase 1: get a free Socrata app token (SOCRATA_APP_TOKEN) -> ~1000 req/hr.
# TODO Phase 1: thread DOB/HPD records on a building to one project_thread_id (Rule 7).
# TODO Phase 1: tune displacement window/severity; gate on organizer sanity-check.
