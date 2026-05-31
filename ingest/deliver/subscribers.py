"""Subscriber signup -> geocode -> location keys (CITY-AGNOSTIC).

Stage: Deliver (Stage 6). Single responsibility: take an email + address at
signup, geocode the address (reusing the shared Normalize geocoder), and persist
the subscriber with their location keys (BBL, lat/lng, community district, ZIP).

Rules honored here:
  - Rule 16 (No premature abstraction): email signup is the ONLY v1 state — no
            accounts, no passwords, no saved searches.
  - Rule 4  (NYC-specific code lives only in nyc/): geocoding is delegated to the
            shared Normalize layer; this module never talks to GeoSupport directly.
  - Rule 15 (SoR key): BBL is stored as the cross-source join key.

CITY-AGNOSTIC: the geocoder happens to return NYC BBL/CD today, but this module
just stores whatever the Normalize layer hands back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection


def add_subscriber(conn: Connection, email: str, address: str) -> dict[str, Any]:
    """Register a subscriber: geocode their address and persist location keys.

    Contract: geocode ``address`` via the shared Normalize geocoder (BBL, lat/lng,
    community_district, ZIP), then INSERT a row in ``subscribers`` (Rule 16: email
    signup is the only state). Returns the stored subscriber record. Double opt-in
    confirmation is a separate step.
    """
    raise NotImplementedError(
        "Phase 2: geocode via normalize, INSERT into subscribers (see schema.sql). "
        "Reuse the Normalize geocoder — do not call GeoSupport here (Rule 4)."
    )

    # TODO Phase 2: double opt-in — issue a confirmation token, set confirmed_at
    # only after the subscriber clicks through.
