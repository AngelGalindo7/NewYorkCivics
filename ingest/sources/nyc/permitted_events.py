"""Stage 1 (Fetch) — NYC permitted street events via Socrata. NYC-SPECIFIC, STRUCTURED.

Pulls the rolling 30-day forward window of NYC-permitted events (block parties,
street fairs, film shoots, markets, cultural events) from NYC Open Data (tvpp-9vvx)
and maps them to the canonical CivicEvent shape.

Source limitation: tvpp-9vvx carries no community district field and no lat/lon
coordinates.  To filter to the target neighborhood, we geocode each event's
event_location address via the GeoSearch HTTP fallback (no extra deps, same path
as normalize/geocode.py).  Only events whose geocoded community_district matches
the target CD are emitted; geocoding failures are logged and skipped.

No LLM — structured feed (Rule 1).  Every emitted event carries an exact Socrata
row link so a resident can verify the permit against the city record (Rule 3).

Rules honored
-------------
- Rule 1: No LLM; plain-English summaries are deterministic templates.
- Rule 3: Row-exact Socrata citation per event.
- Rule 4: NYC-specific knowledge (dataset id, CD target, borough filter) stays here.
- Rule 10: Structured source -> ACCEPTED; confidence = 1.0.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ingest.extract.schemas import CivicEvent, RecordStatus
from ingest.observability import get_logger
from ingest.sources.nyc import citations

if TYPE_CHECKING:
    from sodapy import Socrata

try:
    from requests.exceptions import RequestException
    from sodapy import Socrata
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
except ImportError:
    RequestException = Exception

    def retry(*args: object, **kwargs: object):  # type: ignore[no-redef]
        def _decorator(func):
            return func

        return _decorator

    def retry_if_exception_type(*args: object, **kwargs: object) -> None:  # type: ignore[no-redef]
        return None

    def stop_after_attempt(*args: object, **kwargs: object) -> None:  # type: ignore[no-redef]
        return None

    def wait_exponential(*args: object, **kwargs: object) -> None:  # type: ignore[no-redef]
        return None


log = get_logger(__name__)

SOURCE_ID = "nyc_permitted_events"
DATASET_ID = "tvpp-9vvx"
SOCRATA_DOMAIN = "data.cityofnewyork.us"
_PAGE = 1000
_TIMEOUT = 60

# Target community district — Manhattan CD11 (East Harlem). GeoSearch returns
# community_district as a 3-digit string (boro digit + 2-digit board), e.g. "111".
TARGET_COMMUNITY_DISTRICT = "111"

# GeoSearch endpoint — same stdlib-only path used by normalize/geocode.py.
# autocomplete is sufficient for full-address strings and returns cd in addendum.pad;
# no extra deps needed.
_GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/autocomplete?text={q}&size=1"


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(RequestException),
)
def _get_page(
    client: Socrata,
    *,
    where: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    return client.get(DATASET_ID, where=where, limit=limit, offset=offset, order=":id")


def _geosearch_cd(address: str) -> str | None:
    """Look up the community district for a free-text address via NYC GeoSearch.

    Returns the 3-digit community_district string (e.g. '111' for Manhattan CD11)
    or None if geocoding fails or produces no result.  Uses stdlib only so CI
    imports cleanly without network access.
    """
    try:
        url = _GEOSEARCH_URL.format(q=urllib.parse.quote(address))
        req = urllib.request.Request(url, headers={"User-Agent": "NewYorkCivics/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        features = data.get("features") or []
        if not features:
            return None
        props = features[0].get("properties") or {}
        # GeoSearch v2 returns addendum.pad.cd as a 3-char community district.
        cd = (props.get("addendum") or {}).get("pad", {}).get("cd")
        if cd:
            return str(cd).zfill(3)
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug("GeoSearch failed for %r: %s", address, exc)
        return None


def _parse_event_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _permitted_event_to_event(rec: Mapping[str, Any]) -> CivicEvent:
    now = datetime.now(UTC)
    event_id = str(rec.get("event_id") or rec.get("permit_id") or "")
    event_name = (rec.get("event_name") or "").strip() or "Permitted event"
    event_type = (rec.get("event_type") or "").strip()
    location = (rec.get("event_location") or "").strip()
    start_dt = _parse_event_dt(rec.get("start_date_time"))
    end_dt = _parse_event_dt(rec.get("end_date_time"))

    type_phrase = f" ({event_type})" if event_type else ""
    location_phrase = f" at {location}" if location else ""
    summary = f"Permitted event{type_phrase}{location_phrase}."

    row_citation = (
        citations.socrata_row(
            DATASET_ID,
            "event_id",
            event_id,
            label=f"NYC event permit #{event_id} (NYC Open Data)",
            retrieved_at=now,
        )
        if event_id
        else None
    )

    return CivicEvent(
        source_id=SOURCE_ID,
        source_record_id=event_id,
        action_type="permitted_event",
        title=event_name,
        summary=summary,
        address=location or None,
        event_date=start_dt.date() if start_dt else None,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[row_citation] if row_citation else [],
        extras={
            "event_type": event_type or None,
            "end_date": end_dt.date().isoformat() if end_dt else None,
            "contact_phone": rec.get("event_contact_phone") or None,
            "contact_email": rec.get("event_contact_e_mail") or None,
            "event_borough": rec.get("event_borough") or None,
        },
        extracted_at=now,
    )


def iter_permitted_events(
    community_district: str = TARGET_COMMUNITY_DISTRICT,
    *,
    limit: int | None = None,
    socrata_token: str | None = None,
) -> Iterator[CivicEvent]:
    """Yield upcoming permitted events in the target community district.

    Pulls all Manhattan events from the rolling 30-day forward window and geocodes
    each address to filter to the target CD.  Geocoding failures are skipped so a
    single bad address doesn't block the rest.

    Args:
        community_district: 3-digit CD string to keep (default '111' = Manhattan CD11).
        limit: Optional cap on emitted events (for demos/tests).
        socrata_token: Optional Socrata app token.
    """
    client = Socrata(SOCRATA_DOMAIN, socrata_token, timeout=_TIMEOUT)
    # Filter to Manhattan up-front to bound network and geocoding cost.
    where = "event_borough = 'Manhattan'"
    emitted = 0
    offset = 0
    first_page_empty = True
    try:
        while True:
            page = _get_page(client, where=where, limit=_PAGE, offset=offset)
            if not page:
                break
            first_page_empty = False
            for rec in page:
                location = (rec.get("event_location") or "").strip()
                if not location:
                    continue
                cd = _geosearch_cd(location)
                if cd != community_district:
                    continue
                yield _permitted_event_to_event(rec)
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
            offset += _PAGE
        if first_page_empty:
            log.warning(
                "%s: Manhattan filter returned 0 rows — check event_borough field values.",
                DATASET_ID,
            )
    finally:
        client.close()

    log.info("permitted_events: yielded %d events in CD %s", emitted, community_district)


def discover_permitted_events(
    community_district: str = TARGET_COMMUNITY_DISTRICT,
    *,
    limit: int = 10,
    socrata_token: str | None = None,
) -> list[CivicEvent]:
    """Return a bounded list of upcoming permitted events for the target CD."""
    from ingest.config import get_settings

    settings = get_settings()
    token = socrata_token or settings.socrata_app_token
    return list(iter_permitted_events(community_district, limit=limit, socrata_token=token))
