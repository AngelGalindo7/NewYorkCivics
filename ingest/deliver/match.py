"""Spatial matching — verified events near a subscriber (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: for one subscriber, return new
VERIFIED events near them, resolved across the three nested radii.

The three nested radii (resolved per event, see docs/DATA_MODEL.md):
  - On your block:        within 250m of the subscriber.
  - In your neighborhood: within 500m AND in the same community district (CD).
  - In your area:         same ZIP AND same CD.

Two entry points:
  - ``match_events`` (Phase 2): the PostGIS query over the stored ``events`` table.
  - ``match_subscriber`` (Phase 1/now): an in-memory bander over CivicEvents pulled
    live from the structured connectors, so the digest runs end-to-end before the
    DB exists. Same band semantics; no PostGIS.

Rules honored here:
  - Rule 2  (Fail fast, don't guess): only events with status='accepted' (or
            explicitly 'unverified' for footnoting) are matched — never quarantined
            or guessed records.
  - Rule 10 (Confidence routing): unverified items may be matched but are flagged
            so the digest can footnote them.

CITY-AGNOSTIC: uses geographic distance + plain CD/ZIP fields; no NYC specifics.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection

    from ingest.extract.schemas import CivicEvent

# The three nested radii, in meters. Outer tiers also require a CD (and ZIP) match.
BLOCK_RADIUS_M = 250
NEIGHBORHOOD_RADIUS_M = 500

# Band keys shared with rank.py / digest.py (reader-facing labels live in digest.py).
BAND_ON_YOUR_BLOCK = "on_your_block"
BAND_IN_YOUR_NEIGHBORHOOD = "in_your_neighborhood"
BAND_IN_YOUR_AREA = "in_your_area"

_EARTH_RADIUS_M = 6_371_000.0


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


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


def match_subscriber(
    subscriber: dict[str, Any],
    events: list[CivicEvent],
) -> dict[str, list[CivicEvent]]:
    """In-memory band of pre-pulled events for one subscriber (Phase 1, no DB).

    Contract: assign each event to the tightest band it qualifies for
    (block < neighborhood < area). Same building (equal BBL) is always
    ``on_your_block``. When both subscriber and event carry coordinates, the band
    comes from the great-circle distance (250 m / 500 m). Events without
    coordinates fall to ``in_your_area``: callers pass events already filtered to
    the subscriber's ZIP + community district (the structured connectors scope to
    one neighborhood today; ``match_events`` does this filter in SQL in Phase 2).
    Returns a dict band -> events; ordering/ranking is rank.py's job.
    """
    bands: dict[str, list[CivicEvent]] = {
        BAND_ON_YOUR_BLOCK: [],
        BAND_IN_YOUR_NEIGHBORHOOD: [],
        BAND_IN_YOUR_AREA: [],
    }
    s_lat, s_lng = subscriber.get("latitude"), subscriber.get("longitude")
    s_bbl = subscriber.get("bbl")
    for ev in events:
        if s_bbl and ev.bbl and ev.bbl == s_bbl:
            bands[BAND_ON_YOUR_BLOCK].append(ev)
            continue
        # All four coords must be present for a distance band (narrowed inline so the
        # haversine call is type-safe); otherwise the event falls to the area band.
        if (
            s_lat is not None
            and s_lng is not None
            and ev.latitude is not None
            and ev.longitude is not None
        ):
            dist = _haversine_m(s_lat, s_lng, ev.latitude, ev.longitude)
            if dist <= BLOCK_RADIUS_M:
                bands[BAND_ON_YOUR_BLOCK].append(ev)
            elif dist <= NEIGHBORHOOD_RADIUS_M:
                bands[BAND_IN_YOUR_NEIGHBORHOOD].append(ev)
            else:
                bands[BAND_IN_YOUR_AREA].append(ev)
        else:
            bands[BAND_IN_YOUR_AREA].append(ev)
    return bands
