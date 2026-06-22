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
``nyc``; the base URL is https://webapi.legistar.com/v1/nyc. The REST API now returns
403 to keyless callers (the vendor locked the public endpoint down and there is no
self-serve token), so when it is unavailable the connector falls back to scraping the
public web calendar at https://legistar.council.nyc.gov/Calendar.aspx — same meetings,
same Event shape — behind the same ``discover_*`` interface.
"""

from __future__ import annotations

import re
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


# Separate guard: the web-calendar fallback parses HTML with BeautifulSoup. Keep this
# independent of the httpx/tenacity guard so missing bs4 disables only the fallback parse,
# not the API path — and so the module still imports clean in CI with only pydantic present.
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


from ingest.config import get_settings
from ingest.extract.schemas import Citation, CivicEvent, RecordStatus
from ingest.observability import get_logger

log = get_logger(__name__)

SOURCE_ID = "nyc_legistar"

_BASE = "https://webapi.legistar.com/v1/nyc"
_CALENDAR_URL = "https://legistar.council.nyc.gov/Calendar.aspx"  # web-calendar fallback
_PAGE = 200  # safe page size; Legistar defaults to 1000 but smaller is faster
_TIMEOUT = 30.0  # seconds; Legistar is reasonably fast

# The Legistar Web API now rejects keyless callers with a 403 (the vendor locked the public
# endpoint down), so the keyless API attempt typically fails and triggers the web-calendar
# fallback. The descriptive User-Agent is still sent on the API attempt — it avoids naive
# bot-blocking and is reused for the web-calendar fetch; Accept pins JSON for the API.
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

    A keyless call is still attempted when no token is set, but the public API now typically
    answers keyless callers with a 403, which triggers the web-calendar fallback upstream. A
    token, when supplied by the vendor, authorizes the request and lifts rate limits.
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


# ── Web-calendar fallback ─────────────────────────────────────────────────────
#
# The REST API now returns 403 to keyless callers. The public web calendar lists the same
# meetings, so we scrape it and reshape each grid row into the SAME dict keys the REST API
# returns — letting ``_event_to_civic`` map scraped and API rows identically. The calendar
# is a Telerik RadGrid; the only reliable anchor per meeting is its MeetingDetail link, whose
# ``ID`` query param is the Legistar EventId, so we locate rows via that anchor rather than by
# the grid's element id (which resolves to a wrapper div under the stdlib HTML parser).


def _us_date_to_iso(text: str) -> str | None:
    """Convert a calendar 'MM/DD/YYYY' date to an ISO datetime string, or None."""
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").strftime("%Y-%m-%dT00:00:00")
    except (ValueError, AttributeError):
        return None


def _parse_calendar_row(row: Any, detail: Any) -> dict[str, Any] | None:
    """Map one web-calendar grid row to an API-shaped Event dict, or None to skip."""
    m = re.search(r"[?&]ID=(\d+)", detail.get("href") or "")
    if not m:
        return None
    date_el = row.find("td", class_="rgSorted")
    iso_date = _us_date_to_iso(date_el.get_text(strip=True)) if date_el else None
    if iso_date is None:
        return None  # a forward-looking calendar entry is useless without a date
    body_el = row.find("a", id=re.compile(r"_hypBody$"))
    time_el = row.find("span", id=re.compile(r"_lblTime$"))
    cells = row.find_all("td", recursive=False)
    location = cells[4].get_text(" ", strip=True) if len(cells) > 4 else ""
    return {
        "EventId": int(m.group(1)),
        "EventBodyName": body_el.get_text(strip=True) if body_el else "",
        "EventDate": iso_date,
        "EventTime": time_el.get_text(strip=True) if time_el else None,
        "EventLocation": location,
        "EventAgendaStatusName": "",
    }


def _parse_calendar_html(html: str) -> list[dict[str, Any]]:
    """Parse the Legistar web-calendar HTML into API-shaped Event dicts."""
    if BeautifulSoup is None:
        raise RuntimeError(
            "beautifulsoup4 is not installed; install it with: pip install beautifulsoup4"
        )
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    for detail in soup.find_all("a", id=re.compile(r"_hypMeetingDetail$")):
        row = detail.find_parent("tr")
        if row is None:
            continue
        ev = _parse_calendar_row(row, detail)
        if ev is not None:
            events.append(ev)
    return events


def _scrape_calendar_events() -> list[dict[str, Any]]:
    """Fetch and parse the public Legistar web calendar into API-shaped Event dicts."""
    if httpx is None:
        raise RuntimeError("httpx is not installed; install it with: pip install httpx")
    with httpx.Client(timeout=_TIMEOUT, headers=_HEADERS) as client:
        resp = client.get(_CALENDAR_URL, follow_redirects=True)
        resp.raise_for_status()
    return _parse_calendar_html(resp.text)


def _event_in_window(event: dict[str, Any], start: datetime, end: datetime | None) -> bool:
    """True if the event's date falls within ``[start, end]`` (open-ended when ``end`` is None)."""
    d = _parse_event_date(event.get("EventDate"))
    if d is None or d < start.date():
        return False
    return end is None or d <= end.date()


def _fetch_events(start: datetime, end: datetime | None) -> list[dict[str, Any]]:
    """Return raw Legistar Event dicts in [start, end], API-first, web-calendar fallback.

    The REST API now rejects keyless callers (403). When it is unavailable we scrape the
    public web calendar, which lists the same meetings in the same Event shape, then filter
    client-side to the window. Fails soft: returns [] if both paths fail.
    """
    if end is None:
        filter_expr = f"EventDate ge datetime'{start.strftime('%Y-%m-%dT00:00:00')}'"
    else:
        filter_expr = (
            f"EventDate ge datetime'{start.strftime('%Y-%m-%dT00:00:00')}'"
            f" and EventDate le datetime'{end.strftime('%Y-%m-%dT23:59:59')}'"
        )
    try:
        return _get_all("/Events", **{"$filter": filter_expr, "$orderby": "EventDate"})
    except Exception as exc:  # noqa: BLE001 — any API failure falls through to the web calendar
        log.warning("Legistar REST API unavailable (%s); falling back to web calendar", exc)
    try:
        scraped = _scrape_calendar_events()
    except Exception as exc:  # noqa: BLE001 — the fallback must never break the digest
        log.error("Legistar web-calendar fallback failed: %s", exc)
        return []
    in_window = [e for e in scraped if _event_in_window(e, start, end)]
    # The web calendar lists meetings newest-first; sort ascending so the scrape path matches
    # the REST API's $orderby and honors discover_cd_hearings' "ordered by event_date" contract.
    in_window.sort(key=lambda e: _parse_event_date(e.get("EventDate")) or date.max)
    return in_window


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
    if since is None:
        start = datetime.now(UTC)
    else:
        iso = since if "T" in since else f"{since}T00:00:00"
        try:
            parsed = datetime.fromisoformat(iso.rstrip("Z"))
            start = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            log.warning("unparseable since=%r; defaulting to now (UTC)", since)
            start = datetime.now(UTC)

    for raw in _fetch_events(start, None):
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
    events = _fetch_events(now, now + timedelta(days=days_ahead))

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


# ── Agenda enrichment ────────────────────────────────────────────────────────

_VOTE_MATTER_KEYWORDS = frozenset(
    {
        "housing",
        "land use",
        "zoning",
        "rezoning",
        "affordable",
        "tenant",
        "displacement",
        "ulurp",
        "landmark",
        "special permit",
        "variance",
    }
)


def _matches_vote_keywords(title: str) -> bool:
    lower = title.lower()
    return any(kw in lower for kw in _VOTE_MATTER_KEYWORDS)


def _fetch_event_items(client: httpx.Client, event_id: int) -> list[dict[str, Any]]:
    """Fetch all EventItems for ``event_id`` using an already-open ``client``."""
    if httpx is None:
        raise RuntimeError("httpx is not installed; install it with: pip install httpx")
    token_params = _request_params(get_settings().legistar_token)
    rows: list[dict[str, Any]] = []
    skip = 0
    while True:
        page = _get_page(
            client,
            f"/Events/{event_id}/EventItems",
            {"$top": _PAGE, "$skip": skip, **token_params},
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < _PAGE:
            break
        skip += len(page)
    return rows


def enrich_with_agenda(event: CivicEvent) -> CivicEvent:
    """Return a copy of ``event`` with agenda item titles appended to its summary.

    Fetches ``GET /Events/{event_id}/EventItems`` for the event referenced by
    ``event.source_record_id`` (format: ``event:{EventId}``), collects
    ``EventItemTitle`` strings, and appends them as a bullet list.  The raw list
    is stored in ``event.extras["agenda_items"]``.  The original event is never
    mutated.

    Raises:
        ValueError: if ``source_record_id`` does not match ``event:{int}``.
        RuntimeError: if httpx is not installed.
    """
    if httpx is None:
        raise RuntimeError("httpx is not installed; install it with: pip install httpx")
    parts = event.source_record_id.split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        raise ValueError(f"Cannot parse event_id from source_record_id={event.source_record_id!r}")
    event_id = int(parts[1])
    with httpx.Client(timeout=_TIMEOUT, headers=_HEADERS) as client:
        items = _fetch_event_items(client, event_id)
    titles = [it["EventItemTitle"] for it in items if it.get("EventItemTitle")]
    agenda_block = "Agenda:\n" + "\n".join(f"- {t}" for t in titles) if titles else ""
    new_summary = ((event.summary + "\n\n") if event.summary else "") + agenda_block
    return event.model_copy(
        update={
            "summary": new_summary.strip(),
            "extras": {**event.extras, "agenda_items": titles},
        }
    )


def discover_stated_meeting_votes(event_id: int) -> list[CivicEvent]:
    """Return one CivicEvent per housing/land-use roll-call vote at a Stated Meeting.

    Fetches EventItems for ``event_id``, filters to items whose title matches
    ``_VOTE_MATTER_KEYWORDS``, fetches the per-item vote records, and emits one
    ``council_vote`` CivicEvent per matched item that has at least one vote row.

    Raises:
        RuntimeError: if httpx is not installed.
    """
    if httpx is None:
        raise RuntimeError("httpx is not installed; install it with: pip install httpx")
    token_params = _request_params(get_settings().legistar_token)
    results: list[CivicEvent] = []
    with httpx.Client(timeout=_TIMEOUT, headers=_HEADERS) as client:
        agenda_items = _fetch_event_items(client, event_id)
        for item in agenda_items:
            item_title: str = item.get("EventItemTitle") or ""
            if not _matches_vote_keywords(item_title):
                continue
            item_id: int = item["EventItemId"]
            try:
                votes: list[dict[str, Any]] = []
                skip = 0
                while True:
                    page = _get_page(
                        client,
                        f"/EventItems/{item_id}/Votes",
                        {"$top": _PAGE, "$skip": skip, **token_params},
                    )
                    if not page:
                        break
                    votes.extend(page)
                    if len(page) < _PAGE:
                        break
                    skip += len(page)
            except Exception as exc:  # noqa: BLE001 — fail soft per item
                log.warning("vote fetch skipped for item %d: %s", item_id, exc)
                continue
            if not votes:
                continue
            roll_call = {v["VotePersonName"]: v["VoteValueName"] for v in votes}
            passed: str = item.get("EventItemPassedFlagName") or ""
            tally: str = item.get("EventItemTally") or ""
            title = item_title or f"Agenda item {item_id}"
            summary = (
                f"Council vote on '{title}': {passed}"
                + (f" ({tally})" if tally else "")
                + f". {len(roll_call)} member(s) recorded."
            )
            results.append(
                CivicEvent(
                    source_id=SOURCE_ID,
                    source_record_id=f"item:{item_id}",
                    project_thread_id=f"legistar:event:{event_id}",
                    action_type="council_vote",
                    title=title,
                    summary=summary,
                    confidence=1.0,
                    status=RecordStatus.ACCEPTED,
                    citations=[
                        Citation(
                            kind="data_source",
                            verifies="exact_record",
                            label=f"NYC Council Vote — Item {item_id}",
                            url=f"https://legistar.council.nyc.gov/MeetingDetail.aspx?ID={event_id}",
                            retrieved_at=datetime.now(UTC),
                        )
                    ],
                    extras={
                        "roll_call": roll_call,
                        "event_id": event_id,
                        "event_item_id": item_id,
                        "passed": passed,
                        "tally": tally,
                    },
                    extracted_at=datetime.now(UTC),
                )
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
