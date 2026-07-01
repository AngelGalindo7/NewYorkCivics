"""Contract tests for the Legistar connector.

Runs fully offline against hard-coded Legistar Event / EventItem / Vote dicts.
Verifies: source identity (Rule 15), action_type categorization, confidence
routing (Rule 10), citation audit (Rule 3 / Rule 5), and roll-call mapping.
No network calls; no DB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

import ingest.sources.nyc.legistar as legistar
from ingest.extract.schemas import CivicEvent, RecordStatus
from ingest.sources.nyc.legistar import (
    _HEADERS,
    SOURCE_ID,
    _event_in_window,
    _event_to_civic,
    _fetch_events,
    _HTTPError,
    _parse_calendar_html,
    _parse_event_date,
    _parse_event_time,
    _request_params,
    _us_date_to_iso,
    discover_cd_hearings,
    discover_events,
)

# Representative Legistar Event rows (field names mirror the REST API shape).
SAMPLE_LU_EVENT = {
    "EventId": 73601,
    "EventBodyId": 9,
    "EventBodyName": "Committee on Land Use",
    "EventDate": "2026-06-25T00:00:00",
    "EventTime": "10:00 AM",
    "EventLocation": "City Hall - Committee Room",
    "EventAgendaStatusName": "Final",
    "EventMinutesStatusName": "Not Started",
}

SAMPLE_COUNCIL_EVENT = {
    "EventId": 73700,
    "EventBodyId": 1,
    "EventBodyName": "City Council",
    "EventDate": "2026-07-01T00:00:00",
    "EventTime": "1:30 PM",
    "EventLocation": "City Hall - Council Chambers",
    "EventAgendaStatusName": "Tentative",
    "EventMinutesStatusName": "Not Started",
}

SAMPLE_OTHER_EVENT = {
    "EventId": 73999,
    "EventBodyId": 55,
    "EventBodyName": "Committee on Housing and Buildings",
    "EventDate": "2026-06-20T00:00:00",
    "EventTime": "9:00 AM",
    "EventLocation": "250 Broadway, Room 1620",
    "EventAgendaStatusName": "Tentative",
    "EventMinutesStatusName": "Not Started",
}


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture
def lu_event():
    return _event_to_civic(SAMPLE_LU_EVENT)


@pytest.fixture
def council_event():
    return _event_to_civic(SAMPLE_COUNCIL_EVENT)


@pytest.fixture
def other_event():
    return _event_to_civic(SAMPLE_OTHER_EVENT)


# ── Rule 15: source identity ──────────────────────────────────────────────────


def test_source_id(lu_event):
    assert lu_event.source_id == SOURCE_ID == "nyc_legistar"


def test_source_record_id(lu_event):
    assert lu_event.source_record_id == "event:73601"


def test_project_thread_id(lu_event):
    assert lu_event.project_thread_id == "legistar:event:73601"


# ── Rule 10: confidence routing ───────────────────────────────────────────────


def test_confidence_is_one(lu_event):
    assert lu_event.confidence == 1.0


def test_status_accepted(lu_event, council_event, other_event):
    assert lu_event.status == RecordStatus.ACCEPTED
    assert council_event.status == RecordStatus.ACCEPTED
    assert other_event.status == RecordStatus.ACCEPTED


# ── action_type categorization ────────────────────────────────────────────────


def test_land_use_body_gets_land_use_type(lu_event):
    assert lu_event.action_type == "land_use_hearing"


def test_city_council_gets_council_hearing_type(council_event):
    assert council_event.action_type == "council_hearing"


def test_other_body_gets_council_hearing_type(other_event):
    assert other_event.action_type == "council_hearing"


# ── date / time parsing ───────────────────────────────────────────────────────


def test_event_date_parsed(lu_event):
    assert lu_event.event_date == date(2026, 6, 25)


def test_event_time_parsed(lu_event):
    assert lu_event.event_time == time(10, 0)


def test_event_time_pm(council_event):
    assert council_event.event_time == time(13, 30)


def test_parse_event_date_none():
    assert _parse_event_date(None) is None


def test_parse_event_date_bad():
    assert _parse_event_date("not-a-date") is None


def test_parse_event_time_none():
    assert _parse_event_time(None) is None


def test_parse_event_time_unknown_format():
    assert _parse_event_time("noon") is None


# ── title / summary ───────────────────────────────────────────────────────────


def test_title_contains_body_name(lu_event):
    assert "Committee on Land Use" in lu_event.title


def test_title_contains_date(lu_event):
    assert "2026-06-25" in lu_event.title


def test_summary_contains_location(lu_event):
    assert "City Hall" in lu_event.summary


def test_summary_contains_agenda_status(lu_event):
    assert "Final" in lu_event.summary


# ── citations (Rule 3 / Rule 5) ───────────────────────────────────────────────


def test_citation_present(lu_event):
    assert len(lu_event.citations) == 1


def test_citation_url(lu_event):
    cit = lu_event.citations[0]
    assert "73601" in cit.url
    assert cit.url.startswith("https://legistar.council.nyc.gov/")


def test_citation_kind(lu_event):
    assert lu_event.citations[0].kind == "data_source"


def test_citation_verifies_exact_record(lu_event):
    assert lu_event.citations[0].verifies == "exact_record"


# ── extras ────────────────────────────────────────────────────────────────────


def test_extras_body_name(lu_event):
    assert lu_event.extras["body_name"] == "Committee on Land Use"


def test_extras_agenda_status(lu_event):
    assert lu_event.extras["agenda_status"] == "Final"


def test_extras_location(lu_event):
    assert lu_event.extras["location"] == "City Hall - Committee Room"


# ── no BBL / no address (hearings are not building-level) ─────────────────────


def test_no_bbl(lu_event):
    assert lu_event.bbl is None


def test_no_address(lu_event):
    assert lu_event.address is None


# ── missing optional fields are safe ─────────────────────────────────────────


def test_minimal_event():
    minimal = {"EventId": 1, "EventBodyName": "City Council", "EventDate": "2026-06-30T00:00:00"}
    ev = _event_to_civic(minimal)
    assert ev.source_record_id == "event:1"
    assert ev.event_time is None
    assert ev.status == RecordStatus.ACCEPTED


# ── request param / header shaping (token optional; live API now 403s keyless) ────


def test_request_params_omits_token_when_absent():
    # A missing token must not add a token param — the request shape is token-optional.
    # (The live API now 403s keyless callers, which is what triggers the scrape fallback.)
    params = _request_params(None, **{"$filter": "EventDate ge x", "$orderby": "EventDate"})
    assert "token" not in params
    assert params["$filter"] == "EventDate ge x"
    assert params["$orderby"] == "EventDate"


def test_request_params_includes_token_when_present():
    # A token, when set, is passed through (it only lifts rate limits).
    params = _request_params("tok-123", **{"$top": 50})
    assert params["token"] == "tok-123"
    assert params["$top"] == 50


def test_request_params_empty_when_no_token_no_extra():
    assert _request_params(None) == {}


def test_headers_carry_a_user_agent():
    # A descriptive User-Agent is sent on the API attempt and reused for the web-calendar fetch.
    assert _HEADERS.get("User-Agent")
    assert "nyc-civic-ingest" in _HEADERS["User-Agent"]


# ══════════════════════════════════════════════════════════════════════════════
# Web-calendar fallback (REST API now 403s keyless callers)
# ══════════════════════════════════════════════════════════════════════════════
#
# Two import environments matter here:
#   * CI installs ONLY pydantic — httpx and bs4 are ABSENT, so this module must
#     import and collect cleanly. The pure-logic tests below run there too.
#   * the local dev venv has httpx + bs4 — the bs4 parser tests RUN here and are
#     SKIPPED in CI via ``pytest.importorskip("bs4")``. That is intentional.
#
# The API-403 simulation raises the module's bound ``_HTTPError`` so the module's
# ``except _HTTPError`` always catches it in BOTH environments (in CI it is
# ``Exception``; in dev it is ``httpx.HTTPError``).


# ── fixtures / shaped rows ────────────────────────────────────────────────────


# The 4 web-calendar rows in fixtures/legistar_calendar.html, reshaped to the
# API Event dict keys ``_event_to_civic`` consumes. Only "City Council" (1418199)
# and "Land Use" (1416853) survive the discover_cd_hearings keyword filter.
CAL_EVENTS = [
    {
        "EventId": 1418199,
        "EventBodyName": "City Council Stated Meeting",
        "EventDate": "2026-12-17T00:00:00",
        "EventTime": "1:30 PM",
        "EventLocation": "Council Chambers - City Hall",
        "EventAgendaStatusName": "",
    },
    {
        "EventId": 1417104,
        "EventBodyName": "Subcommittee on Zoning and Franchises",
        "EventDate": "2026-08-12T00:00:00",
        "EventTime": "11:00 AM",
        "EventLocation": "250 Broadway - 8th Floor - Hearing Room 1",
        "EventAgendaStatusName": "",
    },
    {
        "EventId": 1416853,
        "EventBodyName": "Committee on Land Use",
        "EventDate": "2026-08-04T00:00:00",
        "EventTime": "12:00 PM",
        "EventLocation": "250 Broadway - 8th Floor - Hearing Room 1",
        "EventAgendaStatusName": "",
    },
    {
        "EventId": 1421743,
        "EventBodyName": "Committee on Finance",
        "EventDate": "2026-06-11T00:00:00",
        "EventTime": "11:00 AM",
        "EventLocation": "250 Broadway - 8th Floor - Hearing Room 2 VOTE*",
        "EventAgendaStatusName": "",
    },
]


@pytest.fixture
def calendar_html():
    """The trimmed real calendar capture (4 rows)."""
    path = Path(__file__).parent / "fixtures" / "legistar_calendar.html"
    return path.read_text(encoding="utf-8")


def _boom(*args, **kwargs):
    """A scrape stand-in that fails loudly if the API path was supposed to win."""
    raise AssertionError("_scrape_calendar_events must not be called when the API succeeds")


# ── _us_date_to_iso ───────────────────────────────────────────────────────────


def test_us_date_to_iso_valid():
    assert _us_date_to_iso("12/17/2026") == "2026-12-17T00:00:00"


def test_us_date_to_iso_valid_other():
    assert _us_date_to_iso("8/4/2026") == "2026-08-04T00:00:00"


def test_us_date_to_iso_invalid():
    assert _us_date_to_iso("not-a-date") is None


def test_us_date_to_iso_empty():
    assert _us_date_to_iso("") is None


# ── _event_in_window ──────────────────────────────────────────────────────────


def test_event_in_window_inside():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)
    assert _event_in_window({"EventDate": "2026-06-15T00:00:00"}, start, end) is True


def test_event_in_window_before_start():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)
    assert _event_in_window({"EventDate": "2026-05-31T00:00:00"}, start, end) is False


def test_event_in_window_after_end():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)
    assert _event_in_window({"EventDate": "2026-07-01T00:00:00"}, start, end) is False


def test_event_in_window_open_ended_end_none():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    # end=None means open-ended — a far-future date still qualifies.
    assert _event_in_window({"EventDate": "2030-01-01T00:00:00"}, start, None) is True


def test_event_in_window_missing_date_is_false():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    assert _event_in_window({}, start, None) is False


def test_event_in_window_unparseable_date_is_false():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)
    assert _event_in_window({"EventDate": "not-a-date"}, start, end) is False


# ── _fetch_events: API-first, then web-calendar fallback ──────────────────────


def test_fetch_events_api_first_success(monkeypatch):
    # API succeeds -> its rows are returned verbatim and the scraper is NOT touched.
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)
    api_rows = [{"EventId": 1, "EventBodyName": "City Council", "EventDate": "2026-06-10T00:00:00"}]
    monkeypatch.setattr(legistar, "_get_all", lambda *a, **k: api_rows)
    monkeypatch.setattr(legistar, "_scrape_calendar_events", _boom)

    result = _fetch_events(start, end)
    assert result == api_rows


def test_fetch_events_falls_back_to_scrape_on_403(monkeypatch):
    # API 403s -> fall back to the web calendar, then keep only in-window events.
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)

    def _raise_403(*a, **k):
        raise _HTTPError("simulated 403")

    # One event inside the window, one outside — only the in-window one survives,
    # exercising _fetch_events' _event_in_window post-filter on the scrape path.
    scraped = [
        {"EventId": 10, "EventBodyName": "City Council", "EventDate": "2026-06-15T00:00:00"},
        {"EventId": 20, "EventBodyName": "City Council", "EventDate": "2026-07-15T00:00:00"},
    ]
    monkeypatch.setattr(legistar, "_get_all", _raise_403)
    monkeypatch.setattr(legistar, "_scrape_calendar_events", lambda: scraped)

    result = _fetch_events(start, end)
    assert [e["EventId"] for e in result] == [10]


def test_fetch_events_sorts_scrape_results_ascending(monkeypatch):
    # The web calendar lists meetings newest-first; the scrape path must return them
    # ascending by date so discover_cd_hearings' "ordered by event_date" contract holds.
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 12, 31, tzinfo=UTC)

    def _raise_403(*a, **k):
        raise _HTTPError("simulated 403")

    descending = [
        {"EventId": 3, "EventBodyName": "City Council", "EventDate": "2026-12-17T00:00:00"},
        {"EventId": 2, "EventBodyName": "City Council", "EventDate": "2026-08-04T00:00:00"},
        {"EventId": 1, "EventBodyName": "City Council", "EventDate": "2026-06-15T00:00:00"},
    ]
    monkeypatch.setattr(legistar, "_get_all", _raise_403)
    monkeypatch.setattr(legistar, "_scrape_calendar_events", lambda: descending)

    result = _fetch_events(start, end)
    assert [e["EventId"] for e in result] == [1, 2, 3]


def test_fetch_events_fail_soft_returns_empty(monkeypatch):
    # Both paths fail -> return [] rather than raising (the fallback must never break the digest).
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)

    def _raise_403(*a, **k):
        raise _HTTPError("simulated 403")

    def _raise_runtime():
        raise RuntimeError("calendar fetch exploded")

    monkeypatch.setattr(legistar, "_get_all", _raise_403)
    monkeypatch.setattr(legistar, "_scrape_calendar_events", _raise_runtime)

    assert _fetch_events(start, end) == []


# ── discover_cd_hearings: mapping + body keyword filter ───────────────────────


def test_discover_cd_hearings_maps_and_filters(monkeypatch):
    # Patch the narrow fetch seam so this test is free of window/timing dependence.
    # "City Council" (1418199), "Land Use" (1416853), and "Subcommittee on Zoning and
    # Franchises" (1417104, now in _LAND_USE_BODIES) are kept; Committee on Finance is dropped.
    monkeypatch.setattr(legistar, "_fetch_events", lambda start, end: list(CAL_EVENTS))

    results = discover_cd_hearings("MN11", days_ahead=400)
    assert all(isinstance(ev, CivicEvent) for ev in results)
    assert {ev.source_record_id for ev in results} == {
        "event:1418199",
        "event:1416853",
        "event:1417104",
    }
    assert all(ev.status == RecordStatus.ACCEPTED for ev in results)


def test_discover_cd_hearings_fail_soft_empty(monkeypatch):
    monkeypatch.setattr(legistar, "_fetch_events", lambda start, end: [])
    assert discover_cd_hearings("MN11") == []


# ── discover_events: streaming mapping ────────────────────────────────────────


def test_discover_events_maps_each_row(monkeypatch):
    rows = [
        {"EventId": 1, "EventBodyName": "City Council", "EventDate": "2026-07-01T00:00:00"},
        {"EventId": 2, "EventBodyName": "Committee on Land Use", "EventDate": "2026-07-02"},
    ]
    monkeypatch.setattr(legistar, "_fetch_events", lambda start, end: rows)

    events = list(discover_events())
    assert len(events) == 2
    assert all(isinstance(ev, CivicEvent) for ev in events)
    assert all(ev.status == RecordStatus.ACCEPTED for ev in events)
    assert {ev.source_record_id for ev in events} == {"event:1", "event:2"}


# ══════════════════════════════════════════════════════════════════════════════
# bs4 web-calendar PARSER (RUN in the full-dep venv; SKIP in pydantic-only CI)
# ══════════════════════════════════════════════════════════════════════════════


def test_parse_calendar_html_returns_four_events(calendar_html):
    pytest.importorskip("bs4")
    events = _parse_calendar_html(calendar_html)
    assert len(events) == 4
    assert {e["EventId"] for e in events} == {1418199, 1417104, 1416853, 1421743}


def test_parse_calendar_html_maps_row_fields(calendar_html):
    pytest.importorskip("bs4")
    events = _parse_calendar_html(calendar_html)
    council = next(e for e in events if e["EventId"] == 1418199)
    assert council["EventBodyName"] == "City Council Stated Meeting"
    assert council["EventDate"] == "2026-12-17T00:00:00"
    assert council["EventTime"] == "1:30 PM"


def test_parse_calendar_html_end_to_end_to_civic(calendar_html):
    pytest.importorskip("bs4")
    events = _parse_calendar_html(calendar_html)
    by_id = {e["EventId"]: _event_to_civic(e) for e in events}

    council = by_id[1418199]
    assert council.action_type == "council_hearing"
    assert council.status == RecordStatus.ACCEPTED
    assert "1418199" in council.citations[0].url

    land_use = by_id[1416853]
    assert land_use.action_type == "land_use_hearing"
    assert land_use.status == RecordStatus.ACCEPTED
    assert "1416853" in land_use.citations[0].url


def test_parse_calendar_html_captures_guid(calendar_html):
    pytest.importorskip("bs4")
    events = _parse_calendar_html(calendar_html)
    council = next(e for e in events if e["EventId"] == 1418199)
    # The GUID from the MeetingDetail href must be stored so the citation URL can use it.
    assert council["EventGuid"] == "D910E40A-87A1-46F3-9B71-09D6989D9D20"


def test_event_to_civic_uses_guid_in_citation_url(calendar_html):
    pytest.importorskip("bs4")
    events = _parse_calendar_html(calendar_html)
    civic = _event_to_civic(next(e for e in events if e["EventId"] == 1418199))
    url = civic.citations[0].url
    assert "GUID=D910E40A-87A1-46F3-9B71-09D6989D9D20" in url
    assert "Options=info|" in url


def test_event_to_civic_guid_stored_in_extras(calendar_html):
    pytest.importorskip("bs4")
    events = _parse_calendar_html(calendar_html)
    civic = _event_to_civic(next(e for e in events if e["EventId"] == 1418199))
    assert civic.extras["event_guid"] == "D910E40A-87A1-46F3-9B71-09D6989D9D20"


def test_event_to_civic_without_guid_has_basic_url():
    # REST API events have no GUID; citation URL uses just ?ID= so we don't emit a broken link.
    ev = _event_to_civic(SAMPLE_LU_EVENT)
    assert "73601" in ev.citations[0].url
    assert "GUID" not in ev.citations[0].url
    assert ev.extras.get("event_guid") is None


def test_scrape_meeting_agenda_parses_matter_names():
    pytest.importorskip("bs4")
    import ingest.sources.nyc.legistar as _leg

    html = """
    <html><body>
    <tr id="ctl00_ContentPlaceHolder1_gridMain_ctl00__0">
      <td>T2026-100</td><td>1</td><td>Smith</td><td>1</td><td></td>
      <td>Rezoning 123 Main Street (C 260001 ZMM)</td>
      <td>Land Use Application</td>
    </tr>
    <tr id="ctl00_ContentPlaceHolder1_gridMain_ctl00__1">
      <td>T2026-101</td><td>1</td><td>Jones</td><td>2</td><td></td>
      <td>Special permit 456 Broadway (N 260002 ZSM)</td>
      <td>Land Use Application</td>
    </tr>
    </body></html>
    """

    class _FakeResp:
        text = html

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, **kw):
            return _FakeResp()

    import unittest.mock as mock

    with mock.patch("ingest.sources.nyc.legistar.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient()
        matters = _leg._scrape_meeting_agenda(12345, "FAKE-GUID")

    assert matters == [
        "Rezoning 123 Main Street (C 260001 ZMM)",
        "Special permit 456 Broadway (N 260002 ZSM)",
    ]


def test_scrape_meeting_agenda_returns_empty_when_no_rows():
    pytest.importorskip("bs4")
    import unittest.mock as mock

    import ingest.sources.nyc.legistar as _leg

    class _EmptyResp:
        text = "<html><body><table></table></body></html>"

        def raise_for_status(self):
            pass

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, **kw):
            return _EmptyResp()

    with mock.patch("ingest.sources.nyc.legistar.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient()
        assert _leg._scrape_meeting_agenda(99999, "FAKE-GUID") == []


def test_enrich_with_agenda_uses_web_scrape_when_guid_present(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    scraped_matters = ["Rezoning East 116th St", "Special permit variance"]
    monkeypatch.setattr(_leg, "_scrape_meeting_agenda", lambda eid, guid: scraped_matters)

    # Create an event that carries a GUID (as if it came from the web-calendar scraper).
    base = _event_to_civic({**SAMPLE_LU_EVENT, "EventGuid": "FAKE-GUID-123"})
    enriched = _leg.enrich_with_agenda(base)

    assert enriched.extras["agenda_items"] == scraped_matters
    assert "Rezoning East 116th St" in enriched.summary


def test_parse_calendar_html_skips_row_without_id():
    pytest.importorskip("bs4")
    # Detail anchor is present (so the row is visited) but its href carries no ID=,
    # so _parse_calendar_row returns None and the row is skipped.
    html = (
        "<table><tbody><tr class='rgRow'>"
        "<td><a id='x_hypBody'>City Council</a></td>"
        "<td class='rgSorted'>12/17/2026</td>"
        "<td><a href='MeetingDetail.aspx?GUID=abc' id='x_hypMeetingDetail'>Meeting details</a></td>"
        "</tr></tbody></table>"
    )
    assert _parse_calendar_html(html) == []


def test_parse_calendar_html_skips_row_without_date():
    pytest.importorskip("bs4")
    # Anchor has an ID= but the row has no date cell -> useless forward-looking entry, skipped.
    html = (
        "<table><tbody><tr class='rgRow'>"
        "<td><a id='x_hypBody'>City Council</a></td>"
        "<td><a href='MeetingDetail.aspx?ID=999&GUID=abc' id='x_hypMeetingDetail'>details</a></td>"
        "</tr></tbody></table>"
    )
    assert _parse_calendar_html(html) == []


# ══════════════════════════════════════════════════════════════════════════════
# B1 — enrich_with_agenda
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_EVENT_ITEMS = [
    {"EventItemId": 101, "EventItemTitle": "Matter A — Rezoning 123 Main St"},
    {"EventItemId": 102, "EventItemTitle": "Matter B — Special permit application"},
]


def test_enrich_with_agenda_populates_extras_and_summary(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    monkeypatch.setattr(_leg, "_fetch_event_items", lambda client, event_id: SAMPLE_EVENT_ITEMS)
    ev = _event_to_civic(SAMPLE_LU_EVENT)  # source_record_id = "event:73601"
    enriched = _leg.enrich_with_agenda(ev)
    assert enriched.extras["agenda_items"] == [
        "Matter A — Rezoning 123 Main St",
        "Matter B — Special permit application",
    ]
    assert "Matter A" in enriched.summary
    assert "Agenda:" in enriched.summary


def test_enrich_with_agenda_returns_copy_not_mutation(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    monkeypatch.setattr(_leg, "_fetch_event_items", lambda client, event_id: SAMPLE_EVENT_ITEMS)
    ev = _event_to_civic(SAMPLE_LU_EVENT)
    original_summary = ev.summary
    enriched = _leg.enrich_with_agenda(ev)
    assert ev.summary == original_summary  # original unchanged
    assert enriched is not ev  # different object


# ══════════════════════════════════════════════════════════════════════════════
# B2 — discover_stated_meeting_votes
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_EVENT_ITEM_HOUSING = {
    "EventItemId": 201,
    "EventItemTitle": "Int 1234 — Affordable housing tenant protections",
    "EventItemPassedFlagName": "Pass",
    "EventItemTally": "45-4",
    "EventItemMatterTypeName": "Introduction",
}
SAMPLE_EVENT_ITEM_OTHER = {
    "EventItemId": 202,
    "EventItemTitle": "Resolution recognizing National Pizza Day",
    "EventItemPassedFlagName": "Pass",
    "EventItemTally": "49-0",
    "EventItemMatterTypeName": "Resolution",
}
SAMPLE_VOTES = [
    {"VotePersonName": "Rivera", "VoteValueName": "Affirmative"},
    {"VotePersonName": "Powers", "VoteValueName": "Affirmative"},
    {"VotePersonName": "Abreu", "VoteValueName": "Negative"},
]


def test_discover_stated_meeting_votes_filters_keywords_and_emits_events(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    monkeypatch.setattr(
        _leg,
        "_fetch_event_items",
        lambda client, event_id: [SAMPLE_EVENT_ITEM_HOUSING, SAMPLE_EVENT_ITEM_OTHER],
    )
    original_get_page = _leg._get_page

    def mock_get_page(client, path, params):
        if "Votes" in path:
            return SAMPLE_VOTES
        return original_get_page(client, path, params)

    monkeypatch.setattr(_leg, "_get_page", mock_get_page)

    results = _leg.discover_stated_meeting_votes(73601)
    # Only the housing item matches keywords; the pizza resolution does not.
    assert len(results) == 1
    ev = results[0]
    assert ev.action_type == "council_vote"
    assert ev.source_record_id == "item:201"
    assert ev.extras["roll_call"] == {
        "Rivera": "Affirmative",
        "Powers": "Affirmative",
        "Abreu": "Negative",
    }
    assert ev.status == RecordStatus.ACCEPTED


def test_discover_stated_meeting_votes_skips_items_with_no_votes(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    monkeypatch.setattr(
        _leg,
        "_fetch_event_items",
        lambda client, event_id: [SAMPLE_EVENT_ITEM_HOUSING],
    )
    monkeypatch.setattr(_leg, "_get_page", lambda client, path, params: [])  # no votes
    results = _leg.discover_stated_meeting_votes(73601)
    assert results == []


# ══════════════════════════════════════════════════════════════════════════════
# find_matter_by_ulurp
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_MATTER_RESPONSE = [
    {
        "MatterId": 9999,
        "MatterTitle": "C 240042 ZMM - East Harlem Rezoning",
        "MatterStatusName": "Adopted",
        "MatterTypeId": 2,
    }
]


def test_find_matter_by_ulurp_returns_first_match(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return SAMPLE_MATTER_RESPONSE

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, params=None):
            assert "Matters" in url
            assert "C 240042 ZMM" in params.get("$filter", "")
            return _FakeResp()

    monkeypatch.setattr(_leg.httpx, "Client", lambda **kw: _FakeClient())
    result = _leg.find_matter_by_ulurp("C 240042 ZMM")
    assert result is not None
    assert result["MatterId"] == 9999


def test_find_matter_by_ulurp_returns_none_on_empty_response(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, params=None):
            return _FakeResp()

    monkeypatch.setattr(_leg.httpx, "Client", lambda **kw: _FakeClient())
    assert _leg.find_matter_by_ulurp("C 999999 ZMM") is None


def test_find_matter_by_ulurp_returns_none_on_http_error(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, params=None):
            raise _leg._HTTPError("simulated 503")

    monkeypatch.setattr(_leg.httpx, "Client", lambda **kw: _FakeClient())
    assert _leg.find_matter_by_ulurp("C 240042 ZMM") is None


# ══════════════════════════════════════════════════════════════════════════════
# iter_cm_vote_history
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_MATTERS = [
    {
        "MatterId": 301,
        "MatterTitle": "Int 2000 — Affordable housing tenant protections",
        "MatterStatusName": "Adopted",
    },
    {
        "MatterId": 302,
        "MatterTitle": "Resolution re National Pizza Day",
        "MatterStatusName": "Adopted",
    },
]

SAMPLE_MATTER_HISTORIES = [
    {
        "MatterHistoryActionName": "Pass",
        "MatterHistoryActionDate": "2026-03-01T00:00:00",
        "MatterHistoryEventItemId": 401,
    }
]

SAMPLE_HISTORY_VOTES = [
    {"VotePersonName": "Rivera", "VoteValueName": "Affirmative"},
    {"VotePersonName": "Powers", "VoteValueName": "Affirmative"},
]


def test_iter_cm_vote_history_emits_council_vote_events(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    def _mock_get_all(path, **kw):
        if path == "/Matters":
            return SAMPLE_MATTERS
        if "/Histories" in path:
            return SAMPLE_MATTER_HISTORIES
        if "/Votes" in path:
            return SAMPLE_HISTORY_VOTES
        return []

    monkeypatch.setattr(_leg, "_get_all", _mock_get_all)

    results = list(_leg.iter_cm_vote_history(since=date(2026, 1, 1), limit=10))

    assert len(results) == 2
    assert all(ev.action_type == "council_vote" for ev in results)
    assert all(ev.status == RecordStatus.ACCEPTED for ev in results)
    assert all("roll_call" in ev.extras for ev in results)
    assert results[0].extras["roll_call"] == {"Rivera": "Affirmative", "Powers": "Affirmative"}


def test_iter_cm_vote_history_skips_matters_with_no_vote_in_window(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    old_history = [
        {
            "MatterHistoryActionName": "Pass",
            "MatterHistoryActionDate": "2025-01-01T00:00:00",
            "MatterHistoryEventItemId": 401,
        }
    ]

    def _mock_get_all(path, **kw):
        if path == "/Matters":
            return SAMPLE_MATTERS[:1]
        if "/Histories" in path:
            return old_history
        return []

    monkeypatch.setattr(_leg, "_get_all", _mock_get_all)

    results = list(_leg.iter_cm_vote_history(since=date(2026, 1, 1)))
    assert results == []


def test_iter_cm_vote_history_skips_matters_with_no_votes(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    def _mock_get_all(path, **kw):
        if path == "/Matters":
            return SAMPLE_MATTERS[:1]
        if "/Histories" in path:
            return SAMPLE_MATTER_HISTORIES
        if "/Votes" in path:
            return []
        return []

    monkeypatch.setattr(_leg, "_get_all", _mock_get_all)

    results = list(_leg.iter_cm_vote_history(since=date(2026, 1, 1)))
    assert results == []


def test_iter_cm_vote_history_respects_limit(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    def _mock_get_all(path, **kw):
        if path == "/Matters":
            return SAMPLE_MATTERS
        if "/Histories" in path:
            return SAMPLE_MATTER_HISTORIES
        if "/Votes" in path:
            return SAMPLE_HISTORY_VOTES
        return []

    monkeypatch.setattr(_leg, "_get_all", _mock_get_all)

    results = list(_leg.iter_cm_vote_history(since=date(2026, 1, 1), limit=1))
    assert len(results) == 1


def test_iter_cm_vote_history_fails_soft_on_matter_fetch_error(monkeypatch):
    pytest.importorskip("httpx")
    import ingest.sources.nyc.legistar as _leg

    def _boom(path, **kw):
        raise _leg._HTTPError("simulated 403")

    monkeypatch.setattr(_leg, "_get_all", _boom)

    results = list(_leg.iter_cm_vote_history(since=date(2026, 1, 1)))
    assert results == []
