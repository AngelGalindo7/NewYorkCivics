"""Stage 1 (Fetch) — NYC Council / Legistar. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull NYC City Council activity from Legistar — hearings,
Land Use Committee items, and roll-call votes — as clean structured data and map
it to the canonical event shape. Legistar is a structured public API, so this
connector emits records directly and SKIPS Parse and Extract entirely.

Resident value: "what hearing can I still testify at" and "how did my Council
Member vote" come straight from this source with no extraction work.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): structured -> NO LLM, ever.
- Rule 4 (NYC-specific code in nyc/): Legistar endpoint + NYC field mapping stay here.
- Rule 7 (project_thread_id): ``legistar:event:{id}`` / ``legistar:matter:{id}``
  threads hearings to the same ZAP/ULURP story in Phase 2.
- Rule 10 (confidence routing): structured records -> confidence=1.0, ACCEPTED.
- Rule 15 (SoR key): source_record_id = ``event:{EventId}`` or ``matter:{MatterId}``.

Implementation note: python-legistar-scraper is NOT on PyPI (see requirements.txt).
This connector calls the Legistar REST API directly via httpx. NYC's client id is
``nyc``; the base URL is https://webapi.legistar.com/v1/nyc.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

# Import-safety (test_smoke): only pydantic is an import-time dep. The HTTP +
# retry stack is needed only at fetch time — guard so the module imports clean in CI.
try:
    import httpx
    from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

    _HTTPError = httpx.HTTPError
except ImportError:
    httpx = None
    _HTTPError = Exception

    def retry(*args: object, **kwargs: object):
        def _decorator(func):
            return func

        return _decorator

    def retry_if_exception(*args: object, **kwargs: object) -> None:
        return None

    def stop_after_attempt(*args: object, **kwargs: object) -> None:
        return None

    def wait_exponential(*args: object, **kwargs: object) -> None:
        return None


from ingest.config import get_settings
from ingest.extract.schemas import Citation, CivicEvent, RecordStatus
from ingest.observability import get_logger

log = get_logger(__name__)

SOURCE_ID = "nyc_legistar"

_BASE = "https://webapi.legistar.com/v1/nyc"
_PAGE = 200  # safe page size; Legistar defaults to 1000 but smaller is faster
_TIMEOUT = 30.0  # seconds; Legistar is reasonably fast

# The Legistar Web API serves public, read-only data without authentication, so we send
# requests keyless by default. A descriptive User-Agent avoids naive bot-blocking 403s;
# Accept pins JSON.
_HEADERS = {
    "User-Agent": "nyc-civic-ingest/0.1 (+https://github.com/AngelGalindo7/NewYorkCivics)",
    "Accept": "application/json",
}

# Body names that carry land-use hearings residents care about.
_LAND_USE_BODIES = frozenset(
    {
        "Committee on Land Use",
        "Subcommittee on Landmarks, Public Siting and Maritime Uses",
    }
)

# Keyword fragments used in discover_cd_hearings to surface relevant bodies.
_CD_HEARING_KEYWORDS = ("Land Use", "City Council", "Community Board")


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _should_retry(exc: BaseException) -> bool:
    if httpx is not None and isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, _HTTPError)


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_should_retry),
)
def _get_page(client: httpx.Client, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    resp = client.get(f"{_BASE}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def _request_params(token: str | None, **params: Any) -> dict[str, Any]:
    """OData query params for a Legistar request; include the token only when present.

    The public API works keyless, so a missing token is the normal case, not an error — we
    omit it. A token, when set, only lifts rate limits.
    """
    return {"token": token, **params} if token else dict(params)


def _get_all(path: str, **params: Any) -> list[dict[str, Any]]:
    """Paginate a Legistar OData endpoint and return every row.

    Requests are keyless by default; see :func:`_request_params` for token handling.
    """
    if httpx is None:
        raise RuntimeError("httpx is not installed; install it with: pip install httpx")
    query = _request_params(get_settings().legistar_token, **params)
    rows: list[dict[str, Any]] = []
    skip = 0
    with httpx.Client(timeout=_TIMEOUT, headers=_HEADERS) as client:
        while True:
            page = _get_page(client, path, {"$top": _PAGE, "$skip": skip, **query})
            if not page:
                break
            rows.extend(page)
            if len(page) < _PAGE:
                break
            skip += len(page)
    return rows


# ── Field parsers ─────────────────────────────────────────────────────────────


def _parse_event_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z")).date()
    except (ValueError, AttributeError):
        return None


def _parse_event_time(value: str | None) -> time | None:
    """Parse Legistar time strings like '1:30 PM' or '10:00 AM'."""
    if not value:
        return None
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


# ── Event -> CivicEvent mapping ───────────────────────────────────────────────


def _event_to_civic(event: dict[str, Any]) -> CivicEvent:
    """Map one Legistar Event dict to a :class:`~ingest.extract.schemas.CivicEvent`."""
    event_id = str(event["EventId"])
    body_name: str = event.get("EventBodyName") or ""
    event_date = _parse_event_date(event.get("EventDate"))
    location: str = event.get("EventLocation") or ""
    agenda_status: str = event.get("EventAgendaStatusName") or ""
    event_time = _parse_event_time(event.get("EventTime"))

    is_land_use = body_name in _LAND_USE_BODIES
    action_type = "land_use_hearing" if is_land_use else "council_hearing"

    date_str = event_date.isoformat() if event_date else "TBD"
    title = f"{body_name} — {date_str}"
    summary = (
        f"{body_name} hearing scheduled for {date_str}"
        + (f" at {location}" if location else "")
        + (f" (agenda: {agenda_status})" if agenda_status else "")
        + "."
    )

    return CivicEvent(
        source_id=SOURCE_ID,
        source_record_id=f"event:{event_id}",
        project_thread_id=f"legistar:event:{event_id}",
        action_type=action_type,
        title=title,
        summary=summary,
        event_date=event_date,
        event_time=event_time,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label=f"NYC Legistar Event #{event_id}",
                url=f"https://legistar.council.nyc.gov/MeetingDetail.aspx?ID={event_id}",
                retrieved_at=datetime.now(UTC),
            )
        ],
        extras={
            "body_name": body_name,
            "body_id": event.get("EventBodyId"),
            "agenda_status": agenda_status,
            "location": location,
        },
        extracted_at=datetime.now(UTC),
    )


# ── Public API ────────────────────────────────────────────────────────────────


def discover_events(since: str | None = None) -> Iterator[CivicEvent]:
    """Stream upcoming NYC Council hearings and Land Use Committee items.

    Args:
        since: Optional ISO datetime string; yield only events on/after it.
            ``None`` defaults to today (all upcoming events).

    Yields:
        One :class:`~ingest.extract.schemas.CivicEvent` per Legistar Event,
        ``status=ACCEPTED`` (structured, Rule 10), ``source_id=SOURCE_ID``
        (Rule 15). No LLM (Rule 1).
    """
    if since and "T" not in since:
        since = since + "T00:00:00"
    cutoff = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00") if since is None else since

    filter_expr = f"EventDate ge datetime'{cutoff}'"
    try:
        events = _get_all("/Events", **{"$filter": filter_expr, "$orderby": "EventDate"})
    except _HTTPError as exc:
        log.error("Legistar Events fetch failed: %s", exc)
        return

    for raw in events:
        try:
            yield _event_to_civic(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping Legistar event %s: %s", raw.get("EventId"), exc)


def discover_cd_hearings(cd: str, days_ahead: int = 30) -> list[CivicEvent]:
    """Return Land Use Committee and Council hearings in the next ``days_ahead`` days.

    **Phase 1 gate:** "upcoming hearings in CD X returns correct dates for the
    next 30 days." This surfaces every scheduled Land Use Committee and City
    Council session; per-CD scoping via ULURP matter linkage is a Phase 2
    enhancement once EventItems are wired.

    Args:
        cd: Community district code (e.g. ``"MN11"`` for East Harlem).
            Used for log context; full matter-level CD filtering is Phase 2.
        days_ahead: Window size in calendar days (default 30).

    Returns:
        List of :class:`~ingest.extract.schemas.CivicEvent`, ordered by
        ``event_date`` ascending.
    """
    now = datetime.now(UTC)
    start = now.strftime("%Y-%m-%dT00:00:00")
    end = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT23:59:59")
    filter_expr = f"EventDate ge datetime'{start}' and EventDate le datetime'{end}'"

    try:
        events = _get_all("/Events", **{"$filter": filter_expr, "$orderby": "EventDate"})
    except _HTTPError as exc:
        log.error("Legistar CD hearings fetch failed (cd=%s): %s", cd, exc)
        return []

    results: list[CivicEvent] = []
    for raw in events:
        body_name = raw.get("EventBodyName") or ""
        if any(kw in body_name for kw in _CD_HEARING_KEYWORDS):
            try:
                results.append(_event_to_civic(raw))
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping event %s: %s", raw.get("EventId"), exc)

    log.info(
        "discover_cd_hearings(cd=%s, days=%d): %d hearings found",
        cd,
        days_ahead,
        len(results),
    )
    return results


def fetch_roll_call(matter_id: str) -> CivicEvent:
    """Fetch the roll-call vote breakdown for one matter.

    Args:
        matter_id: Legistar matter id (``EventItemMatterId``) whose per-member
            votes are wanted.

    Returns:
        A :class:`~ingest.extract.schemas.CivicEvent` carrying the full
        roll-call (who voted how) in ``extras["roll_call"]``.

    Raises:
        ValueError: if no EventItems reference ``matter_id``.
        RuntimeError: on HTTP failure.
    """
    try:
        items = _get_all(
            "/EventItems",
            **{
                "$filter": f"EventItemMatterId eq {matter_id}",
                "$orderby": "EventItemId desc",
            },
        )
    except _HTTPError as exc:
        raise RuntimeError(
            f"Legistar EventItems fetch for matter {matter_id} failed: {exc}"
        ) from exc

    if not items:
        raise ValueError(f"No EventItems found for matter_id={matter_id!r}")

    # Most recent event item first (ordered by EventItemId desc).
    item = items[0]
    item_id = item["EventItemId"]
    event_id = item.get("EventItemEventId", "")

    try:
        votes = _get_all(f"/EventItems/{item_id}/Votes")
    except _HTTPError as exc:
        raise RuntimeError(f"Legistar Votes fetch for item {item_id} failed: {exc}") from exc

    roll_call = {v["VotePersonName"]: v["VoteValueName"] for v in votes}
    passed = item.get("EventItemPassedFlagName") or ""
    tally = item.get("EventItemTally") or ""

    title = item.get("EventItemTitle") or item.get("EventItemMatterName") or f"Matter {matter_id}"
    summary = (
        f"Roll-call vote on '{title}': {passed}"
        + (f" ({tally})" if tally else "")
        + f". {len(roll_call)} member(s) recorded."
    )

    return CivicEvent(
        source_id=SOURCE_ID,
        source_record_id=f"matter:{matter_id}",
        project_thread_id=f"legistar:matter:{matter_id}",
        action_type="council_vote",
        title=title,
        summary=summary,
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=[
            Citation(
                kind="data_source",
                verifies="exact_record",
                label=f"NYC Council Roll Call — Matter {matter_id}",
                url=(f"https://legistar.council.nyc.gov/LegislationDetail.aspx?ID={matter_id}"),
                retrieved_at=datetime.now(UTC),
            )
        ],
        extras={
            "matter_id": matter_id,
            "event_item_id": item_id,
            "event_id": event_id,
            "passed": passed,
            "tally": tally,
            "roll_call": roll_call,
        },
        extracted_at=datetime.now(UTC),
    )
