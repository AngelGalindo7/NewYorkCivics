"""Spatial matching — verified events near a subscriber (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: for one subscriber, run a PostGIS
radius query that returns new VERIFIED events near them, resolved across the three
nested radii.

The three nested radii (resolved per event, see docs/DATA_MODEL.md):
  - On your block:        within 250m of the subscriber.
  - In your neighborhood: within 500m AND in the same community district (CD).
  - In your area:         same ZIP AND same CD.

Rules honored here:
  - Rule 2  (Fail fast, don't guess): only events with status='accepted' (or
            explicitly 'unverified' for footnoting) are matched — never quarantined
            or guessed records.
  - Rule 10 (Confidence routing): unverified items may be matched but are flagged
            so the digest can footnote them.

CITY-AGNOSTIC: uses PostGIS geography + plain CD/ZIP fields; no NYC specifics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection

# The three nested radii, in meters. Outer tiers also require a CD (and ZIP) match.
BLOCK_RADIUS_M = 250
NEIGHBORHOOD_RADIUS_M = 500


def match_events(
    conn: Connection,
    subscriber: dict[str, Any],
    *,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Return new verified events near ``subscriber``, tagged by radius tier.

    Contract: PostGIS ``<->`` / ``ST_DWithin`` query over ``events.geom`` against
    the subscriber's point, applying the three nested radii (250m; 500m+CD;
    ZIP+CD). ``since`` bounds to events new since the last digest. Each returned
    event carries its matched tier so the ranker (rank.py) can weight proximity.
    Only ``status`` in ('accepted','unverified') is eligible (Rule 2 / Rule 10).
    """
    raise NotImplementedError(
        "Phase 2: ST_DWithin over events.geom with the three nested radii; filter "
        "status IN ('accepted','unverified') and event_date >= since."
    )
