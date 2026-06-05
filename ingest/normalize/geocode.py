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
`GEOSUPPORT_GS_LIBRARY_PATH`. ``geocode()`` returns ``GeoResult(ok=False)``
when binaries are absent so callers quarantine the record rather than crashing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ingest.observability import get_logger

log = get_logger(__name__)

# Borough code mapping (GeoSupport uses 1-digit borough codes or 2-letter codes).
_BOROUGH_MAP: dict[str, str] = {
    "manhattan": "MN",
    "bronx": "BX",
    "brooklyn": "BK",
    "queens": "QN",
    "staten island": "SI",
    "new york": "MN",  # "New York, NY" -> Manhattan
}

# NYC ZIP -> borough code (covers the five borough ZIPs at the first-two-digits level).
_ZIP_BOROUGH: dict[str, str] = {
    "100": "MN",
    "101": "MN",
    "102": "MN",  # Manhattan
    "104": "BX",  # Bronx
    "112": "BK",  # Brooklyn
    "113": "QN",
    "114": "QN",
    "116": "QN",  # Queens
    "103": "SI",  # Staten Island
}


@dataclass(frozen=True)
class GeoResult:
    """Result of geocoding one address against NYC GeoSupport.

    A failed geocode (``ok is False``) carries no BBL/BIN and routes the record
    to quarantine (Rule 2) rather than into ``events``.
    """

    ok: bool
    bbl: str | None = None  # Borough-Block-Lot (cross-source join key, Rule 15)
    bin: str | None = None  # Building Identification Number
    community_district: str | None = None  # e.g. "111" for Manhattan CD 11
    latitude: float | None = None  # WGS84
    longitude: float | None = None  # WGS84
    reason: str | None = None  # quarantine reason when ok is False


def _detect_borough(address: str) -> str:
    """Return a GeoSupport 2-letter borough code from a free-text address."""
    lower = address.lower()
    for name, code in _BOROUGH_MAP.items():
        if name in lower:
            return code
    # Try ZIP
    m = re.search(r"\b(\d{5})\b", address)
    if m:
        prefix = m.group(1)[:3]
        if prefix in _ZIP_BOROUGH:
            return _ZIP_BOROUGH[prefix]
    return "MN"  # East Harlem default


def _split_address(address: str) -> tuple[str, str]:
    """Split 'house_number street_name' from a full address string.

    Returns (house_number, street_name). Handles:
      - "123 East 116th Street, New York, NY 10029"
      - "123 EAST 116 STREET"
      - "1 E 125TH ST"
    """
    # Strip everything after first comma (city/state/zip)
    street_part = address.split(",")[0].strip()
    # Split on first whitespace boundary — house number is always first token
    m = re.match(r"^(\d+[-\w]*)\s+(.+)$", street_part)
    if m:
        return m.group(1), m.group(2)
    return "", street_part


def _try_import_geosupport() -> Any:
    """Return an initialized Geosupport instance or None if not available."""
    try:
        from geosupport import Geosupport  # type: ignore[import-untyped]

        return Geosupport()
    except ImportError:
        log.debug("geosupport package not installed; geocoding unavailable")
        return None
    except Exception as exc:
        # Catches missing binaries / unset GEOFILES env at init time (OSError, etc.).
        # This is NOT a normal "optional dep absent" case — the package installed but
        # the binaries or env are broken, so warn rather than silently swallowing it.
        log.warning("GeoSupport init failed (binaries/env not configured): %s", exc)
        return None


# Module-level singleton — initialized once; None when binaries are absent.
_GS: Any = _try_import_geosupport()


def geocode(address: str) -> GeoResult:
    """Resolve ``address`` to NYC identifiers via GeoSupport (Function 1B).

    Contract: on success returns ``GeoResult(ok=True, bbl=..., ...)``; on any
    unresolved/ambiguous address returns ``GeoResult(ok=False, reason=...)`` so
    the caller quarantines the record (Rule 2). Never invents a BBL.

    Returns ``ok=False`` (reason: "GeoSupport not configured") when the
    GeoSupport binaries or GEOFILES env are absent — routes to quarantine rather
    than crashing the pipeline. Set up GeoSupport then re-run to resolve.

    Requires GeoSupport binaries + GEOSUPPORT_* env (see module docstring and
    .env.example).
    """
    if _GS is None:
        return GeoResult(
            ok=False,
            reason=(
                "GeoSupport not configured: install binaries and set "
                "GEOSUPPORT_GEOFILES + GEOSUPPORT_GS_LIBRARY_PATH (see .env.example)"
            ),
        )

    borough_code = _detect_borough(address)
    house_number, street_name = _split_address(address)

    if not house_number or not street_name:
        return GeoResult(ok=False, reason=f"could not parse house_number from {address!r}")

    try:
        result: dict[str, Any] = _GS["1B"](
            house_number=house_number,
            street_name=street_name,
            borough_code=borough_code,
        )
    except Exception as exc:
        reason = str(exc)
        log.debug("GeoSupport 1B failed for %r: %s", address, reason)
        return GeoResult(ok=False, reason=reason)

    bbl_raw = result.get("BOROUGH BLOCK LOT (BBL)") or ""
    bbl = bbl_raw.replace("-", "").strip() or None

    bin_raw = result.get("Building Identification Number (BIN) of Input Address or NAP") or ""
    bin_val = bin_raw.strip() or None

    cd_raw = result.get("Community District") or ""
    community_district = cd_raw.strip() or None

    lat_raw = result.get("Latitude") or ""
    lon_raw = result.get("Longitude") or ""
    try:
        lat = float(lat_raw) if lat_raw else None
        lon = float(lon_raw) if lon_raw else None
    except ValueError:
        lat = lon = None

    if not bbl:
        return GeoResult(
            ok=False,
            reason=f"GeoSupport returned no BBL for {address!r}",
        )

    log.debug(
        "geocode(%r) -> BBL=%s CD=%s lat=%.4f lon=%.4f",
        address,
        bbl,
        community_district,
        lat or 0.0,
        lon or 0.0,
    )
    return GeoResult(
        ok=True,
        bbl=bbl,
        bin=bin_val,
        community_district=community_district,
        latitude=lat,
        longitude=lon,
    )
