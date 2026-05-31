"""Address -> BBL/BIN/Community District via NYC GeoSupport.

Stage: Normalize & validate (stage 4).
Single responsibility: resolve a street address to NYC geographic identifiers
(BBL, BIN, Community District, lat/lng) using the official NYC GeoSupport
geocoder, so records can be joined cross-source on BBL and matched to
subscribers by location.

Boundary: this module CALLS an NYC-SPECIFIC service (GeoSupport). The
``GeoResult`` shape is plain/portable; the geocoder behind it is NYC-only. To
port to another city, swap this implementation for that city's geocoder behind
the same contract (Rule 14 — copy-and-swap).

Rules honored:
- Rule 2 (fail fast, don't guess): an address that GeoSupport cannot resolve
  returns a failure result (no BBL) so Normalize quarantines it — we never
  fabricate a BBL.
- Rule 15 (SoR key): BBL is the cross-source join key this stage produces.

Geocoder choice: **NYC GeoSupport** (`geosupport` wrapper). Free, official,
returns BBL directly. **NOT Google / Mapbox.**

SETUP CAVEAT: GeoSupport is not pure pip. It requires the GeoSupport binaries
plus environment variables `GEOSUPPORT_GEOFILES` and
`GEOSUPPORT_GS_LIBRARY_PATH`. ``geocode()`` cannot run until these are present.

# TODO Phase 1: wire the `geosupport` wrapper; add the geocoding eval (100
# addresses, median <50m, p95 <500m) per docs/EVAL.md before relying on output.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GeoResult:
    """Result of geocoding one address against NYC GeoSupport.

    A failed geocode (``ok is False``) carries no BBL/BIN and routes the record
    to quarantine (Rule 2) rather than into ``events``.
    """

    ok: bool
    bbl: str | None = None  # Borough-Block-Lot (cross-source join key, Rule 15)
    bin: str | None = None  # Building Identification Number
    community_district: str | None = None  # e.g. "MN07"
    latitude: float | None = None  # WGS84
    longitude: float | None = None  # WGS84
    reason: str | None = None  # quarantine reason when ok is False


def geocode(address: str) -> GeoResult:
    """Resolve ``address`` to NYC identifiers via GeoSupport.

    Contract: on success returns ``GeoResult(ok=True, bbl=..., ...)``; on any
    unresolved/ambiguous address returns ``GeoResult(ok=False, reason=...)`` so
    the caller quarantines the record (Rule 2). Never invents a BBL.

    Requires GeoSupport binaries + GEOSUPPORT_* env (see module docstring).
    """
    raise NotImplementedError(
        "Phase 1: call the geosupport wrapper; requires GeoSupport binaries + GEOSUPPORT_* env."
    )
