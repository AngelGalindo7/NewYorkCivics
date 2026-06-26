"""Subscriber signup -> geocode -> CSV store (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: take an email + address at
signup, geocode the address (reusing the shared Normalize geocoder), and persist
the subscriber with their location keys (BBL, lat/lng, community district, ZIP).

Storage: a flat CSV at ``out/subscribers.csv`` (or a caller-supplied path).
No database at this scale — one board, one reader. Swap the store later without
changing the public contract (add_subscriber / load_subscribers).

Rules honored here:
  - Rule 16 (No premature abstraction): CSV is sufficient for one subscriber;
            no accounts, no passwords, no saved searches.
  - Rule 4  (NYC-specific code lives only in nyc/): geocoding is delegated to
            the shared Normalize layer; this module never calls GeoSupport directly.
  - Rule 15 (SoR key): BBL is stored as the cross-source join key when available.
  - Rule 2  (fail fast): an address that cannot be geocoded raises ValueError so
            the caller shows the user an actionable error, not a silent null BBL.

CITY-AGNOSTIC: the geocoder happens to return NYC BBL/CD today, but this module
stores whatever the Normalize layer hands back.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from ingest.normalize.geocode import geocode
from ingest.observability import get_logger

log = get_logger(__name__)

_DEFAULT_CSV = Path("out") / "subscribers.csv"

# Canonical field order — every row written/read uses this list so CSV columns
# are stable across add / load round-trips.
_FIELDS = [
    "email",
    "name",
    "address",
    "bbl",
    "latitude",
    "longitude",
    "zip",
    "community_district",
]


def add_subscriber(
    email: str,
    address: str,
    *,
    name: str | None = None,
    csv_path: Path | None = None,
) -> dict[str, Any]:
    """Geocode ``address`` and persist a subscriber row to CSV.

    Accepts a full street address or a nearby intersection
    (e.g. ``"E 116th St & Lex Ave, New York, NY"``).

    Raises ``ValueError`` if the address cannot be geocoded so the caller can
    surface a clear error to the user rather than silently storing a null BBL.

    Idempotent on ``email``: a second call with the same email replaces the
    prior row (useful for updating an address).
    """
    geo = geocode(address)
    if not geo.ok:
        raise ValueError(
            f"Could not geocode {address!r}: {geo.reason}. "
            "Try a full street address or a nearby intersection."
        )

    subscriber: dict[str, Any] = {
        "email": email,
        "name": name,
        "address": address,
        "bbl": geo.bbl,
        "latitude": geo.latitude,
        "longitude": geo.longitude,
        "zip": _extract_zip(address),
        "community_district": geo.community_district,
    }

    path = csv_path or _DEFAULT_CSV
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_rows(path)
    rows = [r for r in existing if r.get("email") != email]
    rows.append(_to_row(subscriber))

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    log.info("add_subscriber: stored %s at %s", email, address)
    return subscriber


def load_subscribers(csv_path: Path | None = None) -> list[dict[str, Any]]:
    """Return all subscribers from CSV as dicts compatible with ``build_digest``.

    Skips malformed rows with a warning rather than crashing — a corrupt row
    should not block delivery for everyone else.
    """
    path = csv_path or _DEFAULT_CSV
    result: list[dict[str, Any]] = []
    for row in _load_rows(path):
        try:
            result.append(_from_row(row))
        except (KeyError, ValueError) as exc:
            log.warning("load_subscribers: skipping malformed row: %s", exc)
    return result


# ── private helpers ────────────────────────────────────────────────────────────


def _extract_zip(address: str) -> str | None:
    """Pull a 5-digit ZIP code out of a free-text address string."""
    m = re.search(r"\b(\d{5})\b", address)
    return m.group(1) if m else None


def _to_row(sub: dict[str, Any]) -> dict[str, str]:
    """Serialize a subscriber dict to a flat all-string CSV row."""
    return {k: "" if sub.get(k) is None else str(sub[k]) for k in _FIELDS}


def _from_row(row: dict[str, str]) -> dict[str, Any]:
    """Deserialize a CSV row back to a typed subscriber dict."""
    return {
        "email": row["email"],
        "name": row.get("name") or None,
        "address": row["address"],
        "bbl": row.get("bbl") or None,
        "latitude": float(row["latitude"]) if row.get("latitude") else None,
        "longitude": float(row["longitude"]) if row.get("longitude") else None,
        "zip": row.get("zip") or None,
        "community_district": row.get("community_district") or None,
    }


def _load_rows(path: Path) -> list[dict[str, str]]:
    """Read raw CSV rows from ``path``; returns empty list if file absent."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
