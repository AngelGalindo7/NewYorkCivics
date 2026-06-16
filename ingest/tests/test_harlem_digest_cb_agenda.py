"""Offline tests for the CB11-agenda leg of the East Harlem runner.

Runs fully offline (no network, no LLM, no DB): the live feeds and the
discover -> fetch -> Parse -> Extract seam are monkeypatched, so this exercises
only the wiring in :func:`gather_live_events` — that the dirty-PDF leg's events
merge into the digest, and that any failure in the leg fails soft and leaves the
core (non-CB) events untouched.
"""

from __future__ import annotations

import pytest

from ingest.extract.schemas import CivicEvent
from ingest.parse import ParsedDoc
from ingest.sources.nyc import harlem_digest
from ingest.sources.nyc.cb_agenda import AgendaRef


@pytest.fixture
def offline_runner(monkeypatch):
    """Silence every live feed so a call to gather_live_events stays offline.

    The base feeds (HPD/DOB) and the optional ZAP/Legistar pulls are stubbed to a
    single sentinel core event, so a test can assert on what the CB-agenda leg
    adds (or doesn't) on top of a known core.
    """
    core = CivicEvent(source_id="nyc_dob", source_record_id="CORE-1", title="core permit")

    # The base feeds run unconditionally; seed exactly one known core event through the
    # first feed call and leave the rest empty so a test can assert on the CB-agenda delta.
    feeds = iter([[core], []])
    monkeypatch.setattr(harlem_digest, "iter_feed", lambda *a, **k: iter(next(feeds)))
    monkeypatch.setattr(harlem_digest, "iter_zap_events", lambda *a, **k: iter(()))
    monkeypatch.setattr(harlem_digest, "discover_cd_hearings", lambda *a, **k: [])
    return core


def _agenda_ref() -> AgendaRef:
    return AgendaRef(
        board="MN11",
        url="https://example.test/cb11/agenda-2026-06-01.pdf",
        meeting_date="2026-06-01",
        title="CB11 Full Board Meeting June 1, 2026",
    )


def test_cb_agenda_events_merge_into_digest(monkeypatch, offline_runner):
    cb_event = CivicEvent(
        source_id="nyc_cb_mn11",
        source_record_id="nyc_cb_mn11-item-0001",
        title="Rezoning at 123 East 116th Street",
    )

    monkeypatch.setattr(
        harlem_digest.cb_agenda, "discover_agendas", lambda board=None: [_agenda_ref()]
    )
    monkeypatch.setattr(harlem_digest.cb_agenda, "fetch", lambda url: b"%PDF-1.7 fake agenda bytes")
    monkeypatch.setattr(
        harlem_digest.pdf_text, "extract_text", lambda b: ParsedDoc(text="agenda text")
    )
    monkeypatch.setattr(harlem_digest.extractor, "extract", lambda doc, *, source_id: [cb_event])

    events = harlem_digest.gather_live_events(include_cb_agenda=True)

    assert cb_event in events, "CB-agenda event was not merged into the digest"
    assert offline_runner in events, "core event was lost when the CB-agenda leg ran"


def test_cb_agenda_extract_failure_keeps_core(monkeypatch, offline_runner):
    def _boom(doc, *, source_id):
        raise RuntimeError("simulated LLM/extract failure")

    monkeypatch.setattr(
        harlem_digest.cb_agenda, "discover_agendas", lambda board=None: [_agenda_ref()]
    )
    monkeypatch.setattr(harlem_digest.cb_agenda, "fetch", lambda url: b"%PDF-1.7 fake agenda bytes")
    monkeypatch.setattr(
        harlem_digest.pdf_text, "extract_text", lambda b: ParsedDoc(text="agenda text")
    )
    monkeypatch.setattr(harlem_digest.extractor, "extract", _boom)

    events = harlem_digest.gather_live_events(include_cb_agenda=True)

    # Fail-soft: the leg swallowed the extract error and the core event survived.
    assert offline_runner in events
    assert all(ev.source_id != "nyc_cb_mn11" for ev in events)


def test_cb_agenda_off_by_default(monkeypatch, offline_runner):
    def _should_not_run(board=None):
        raise AssertionError("discover_agendas must not run when include_cb_agenda is False")

    monkeypatch.setattr(harlem_digest.cb_agenda, "discover_agendas", _should_not_run)

    events = harlem_digest.gather_live_events()

    assert offline_runner in events
