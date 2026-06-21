"""Offline tests for the ULURP-packet leg of the East Harlem runner.

Runs fully offline (no network, no LLM, no DB): the live feeds and the
discover -> fetch -> Parse -> Extract seam are monkeypatched, so this exercises
only the wiring in :func:`gather_live_events` — that ULURP-packet events merge
into the digest, and that any failure in the leg fails soft and leaves the core
(non-ULURP) events untouched.
"""

from __future__ import annotations

import pytest

from ingest.extract.schemas import CivicEvent
from ingest.parse import ParsedDoc
from ingest.sources.nyc import harlem_digest
from ingest.sources.nyc.ulurp_packet import PacketRef


@pytest.fixture
def offline_runner(monkeypatch):
    """Silence every live feed so gather_live_events stays fully offline.

    Seeds exactly one known core event through the first HPD/DOB feed call;
    ZAP and Legistar are stubbed empty. Tests assert on what the ULURP leg
    adds (or doesn't) on top of this known core.
    """
    core = CivicEvent(source_id="nyc_dob", source_record_id="CORE-1", title="core permit")

    feeds = iter([[core], []])
    monkeypatch.setattr(harlem_digest, "iter_feed", lambda *a, **k: iter(next(feeds)))
    monkeypatch.setattr(harlem_digest, "iter_zap_events", lambda *a, **k: iter(()))
    monkeypatch.setattr(harlem_digest, "discover_cd_hearings", lambda *a, **k: [])
    return core


def _packet_ref() -> PacketRef:
    return PacketRef(
        ulurp_number="C 240042 ZMM",
        url="https://a836-zap.nyc.gov/document/ulurp/C240042ZMM",
        project_thread_id="zap:P2024M0042",
        title="ULURP packet C 240042 ZMM",
    )


# --------------------------------------------------------------------------- #
# Happy path — events merge into digest                                        #
# --------------------------------------------------------------------------- #


def test_ulurp_packet_events_merge_into_digest(monkeypatch, offline_runner):
    ulurp_event = CivicEvent(
        source_id="nyc_ulurp_packet",
        source_record_id="nyc_ulurp_packet-item-0000",
        title="Rezoning at 123 East 116th Street",
    )

    monkeypatch.setattr(
        harlem_digest.ulurp_packet, "discover_packets", lambda *a, **k: [_packet_ref()]
    )
    monkeypatch.setattr(
        harlem_digest.ulurp_packet, "fetch", lambda url: b"%PDF-1.7 fake packet bytes"
    )
    monkeypatch.setattr(
        harlem_digest.pdf_text, "extract_text", lambda b: ParsedDoc(text="packet text")
    )
    monkeypatch.setattr(harlem_digest.extractor, "extract", lambda doc, *, source_id: [ulurp_event])

    events = harlem_digest.gather_live_events(include_ulurp_packet=True)

    assert ulurp_event in events, "ULURP-packet event was not merged into the digest"
    assert offline_runner in events, "core event was lost when the ULURP-packet leg ran"


# --------------------------------------------------------------------------- #
# Fail-soft — extract failure must not discard core events                     #
# --------------------------------------------------------------------------- #


def test_ulurp_packet_extract_failure_keeps_core(monkeypatch, offline_runner):
    def _boom(doc, *, source_id):
        raise RuntimeError("simulated LLM/extract failure")

    monkeypatch.setattr(
        harlem_digest.ulurp_packet, "discover_packets", lambda *a, **k: [_packet_ref()]
    )
    monkeypatch.setattr(
        harlem_digest.ulurp_packet, "fetch", lambda url: b"%PDF-1.7 fake packet bytes"
    )
    monkeypatch.setattr(
        harlem_digest.pdf_text, "extract_text", lambda b: ParsedDoc(text="packet text")
    )
    monkeypatch.setattr(harlem_digest.extractor, "extract", _boom)

    events = harlem_digest.gather_live_events(include_ulurp_packet=True)

    # The extract error is swallowed per-packet; the core event must survive.
    assert offline_runner in events
    assert all(ev.source_id != "nyc_ulurp_packet" for ev in events)


def test_ulurp_packet_fetch_failure_skips_packet_keeps_core(monkeypatch, offline_runner):
    def _bad_fetch(url):
        raise OSError("simulated network failure")

    monkeypatch.setattr(
        harlem_digest.ulurp_packet, "discover_packets", lambda *a, **k: [_packet_ref()]
    )
    monkeypatch.setattr(harlem_digest.ulurp_packet, "fetch", _bad_fetch)

    events = harlem_digest.gather_live_events(include_ulurp_packet=True)

    assert offline_runner in events
    assert all(ev.source_id != "nyc_ulurp_packet" for ev in events)


def test_ulurp_packet_discovery_failure_keeps_core(monkeypatch, offline_runner):
    def _bad_discover(*a, **k):
        raise RuntimeError("simulated discovery failure")

    monkeypatch.setattr(harlem_digest.ulurp_packet, "discover_packets", _bad_discover)

    events = harlem_digest.gather_live_events(include_ulurp_packet=True)

    assert offline_runner in events


# --------------------------------------------------------------------------- #
# Off by default                                                               #
# --------------------------------------------------------------------------- #


def test_ulurp_packet_off_by_default(monkeypatch, offline_runner):
    def _should_not_run(*a, **k):
        raise AssertionError("discover_packets must not run when include_ulurp_packet is False")

    monkeypatch.setattr(harlem_digest.ulurp_packet, "discover_packets", _should_not_run)

    events = harlem_digest.gather_live_events()

    assert offline_runner in events


# --------------------------------------------------------------------------- #
# max_pages truncation                                                         #
# --------------------------------------------------------------------------- #


def test_ulurp_packet_max_pages_truncates_doc(monkeypatch, offline_runner):
    """When the parsed doc has more pages than max_pages, the text is truncated."""
    from ingest.parse import PageLayout

    captured: list[ParsedDoc] = []

    # Build a fake ParsedDoc with 5 pages (each 10 chars) to verify truncation.
    page_texts = ["A" * 10] * 5
    full_text = "\n\n".join(page_texts)
    layout = [PageLayout(page_number=i + 1, char_count=10, is_scanned=False) for i in range(5)]
    big_doc = ParsedDoc(text=full_text, layout=layout)

    monkeypatch.setattr(
        harlem_digest.ulurp_packet, "discover_packets", lambda *a, **k: [_packet_ref()]
    )
    monkeypatch.setattr(harlem_digest.ulurp_packet, "fetch", lambda url: b"%PDF-1.7 fake")
    monkeypatch.setattr(harlem_digest.pdf_text, "extract_text", lambda b: big_doc)

    def _capture(doc, *, source_id):
        captured.append(doc)
        return []

    monkeypatch.setattr(harlem_digest.extractor, "extract", _capture)

    harlem_digest.gather_live_events(include_ulurp_packet=True, max_pages=2)

    assert len(captured) == 1
    doc_sent = captured[0]
    # 2 pages × 10 chars + 1 separator × 2 chars = 22 chars max
    assert len(doc_sent.text) <= 22
    assert len(doc_sent.layout) == 2
