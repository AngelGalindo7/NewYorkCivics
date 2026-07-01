"""Offline tests for the ZAP cross-source corroboration step.

All tests are offline — no network, no LLM, no DB. Corroborate is a pure
function so every case is deterministic: build CivicEvent objects directly,
call corroborate_against_zap, assert on the returned list.
"""

from __future__ import annotations

import pytest

from ingest.extract.schemas import CivicEvent, RecordStatus
from ingest.parse import ParsedDoc
from ingest.sources.nyc import corroborate, harlem_digest
from ingest.sources.nyc.ulurp_packet import PacketRef

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _zap_event(
    project_thread_id: str,
    ulurp_number: str | None,
    source_record_id: str = "P2024M0042",
) -> CivicEvent:
    return CivicEvent(
        source_id="nyc_zap",
        source_record_id=source_record_id,
        project_thread_id=project_thread_id,
        ulurp_number=ulurp_number,
    )


def _dirty_event(
    source_id: str = "nyc_ulurp_packet",
    project_thread_id: str | None = "zap:P2024M0042",
    ulurp_number: str | None = "C 240042 ZMM",
    status: RecordStatus = RecordStatus.REVIEW,
) -> CivicEvent:
    return CivicEvent(
        source_id=source_id,
        source_record_id="packet-item-0000",
        project_thread_id=project_thread_id,
        ulurp_number=ulurp_number,
        status=status,
    )


# --------------------------------------------------------------------------- #
# Core reconciliation logic                                                    #
# --------------------------------------------------------------------------- #


def test_matching_ulurp_accepts_event():
    """Dirty event with matching ULURP and project_thread_id → ACCEPTED."""
    dirty = _dirty_event(ulurp_number="C 240042 ZMM", project_thread_id="zap:P2024M0042")
    zap = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")

    result = corroborate.corroborate_against_zap([dirty], [zap])

    assert len(result) == 1
    assert result[0].status == RecordStatus.ACCEPTED


def test_mismatched_ulurp_stays_review_with_note():
    """ULURP conflict: dirty claims one number but ZAP says another → REVIEW with note."""
    dirty = _dirty_event(ulurp_number="C 240042 ZMM", project_thread_id="zap:P2024M0042")
    zap = _zap_event("zap:P2024M0042", ulurp_number="N 230117 ZRM")

    result = corroborate.corroborate_against_zap([dirty], [zap])

    assert len(result) == 1
    ev = result[0]
    assert ev.status == RecordStatus.REVIEW
    note = ev.extras.get("corroboration_discrepancy", "")
    assert "C 240042 ZMM" in note
    assert "N 230117 ZRM" in note


def test_no_zap_match_unchanged():
    """No matching ZAP record for the thread id → event returned unchanged."""
    dirty = _dirty_event(project_thread_id="zap:UNKNOWN")
    zap = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")

    result = corroborate.corroborate_against_zap([dirty], [zap])

    assert result[0].status == RecordStatus.REVIEW
    assert "corroboration_discrepancy" not in result[0].extras


def test_no_project_thread_id_unchanged():
    """Dirty event without a project_thread_id → passed through without modification."""
    dirty = _dirty_event(project_thread_id=None)
    zap = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")

    result = corroborate.corroborate_against_zap([dirty], [zap])

    assert result[0] is dirty  # same object, not a copy


def test_zap_events_pass_through_unmodified():
    """ZAP source events in the input list are returned without status changes."""
    zap1 = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")
    zap2 = _zap_event("zap:P2023M0117", ulurp_number="N 230117 ZRM", source_record_id="P2023M0117")
    zap_events = [zap1, zap2]

    result = corroborate.corroborate_against_zap(zap_events, zap_events)

    # ZAP events are not dirty sources — they pass through as-is.
    assert result[0] is zap1
    assert result[1] is zap2


def test_dirty_event_without_ulurp_number_accepts_on_thread_match():
    """Thread match with no ULURP to compare on the dirty side → ACCEPTED.

    The LLM may fail to extract the ULURP number while still correctly
    identifying the project thread. A ZAP thread match alone is enough
    to confirm the event is real.
    """
    dirty = _dirty_event(ulurp_number=None, project_thread_id="zap:P2024M0042")
    zap = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")

    result = corroborate.corroborate_against_zap([dirty], [zap])

    assert result[0].status == RecordStatus.ACCEPTED


def test_ulurp_normalization_is_case_and_space_insensitive():
    """Lowercase and extra-spaced ULURP from the LLM matches the canonical ZAP form."""
    dirty = _dirty_event(ulurp_number="c 240042 zmm")  # lowercase + spaces
    zap = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")

    result = corroborate.corroborate_against_zap([dirty], [zap])

    assert result[0].status == RecordStatus.ACCEPTED


def test_cb_agenda_source_also_corroborated():
    """CB-agenda events are dirty sources and should be subject to the same check."""
    dirty = CivicEvent(
        source_id="nyc_cb_mn11",
        source_record_id="mn11-item-0001",
        project_thread_id="zap:P2024M0042",
        ulurp_number="C 240042 ZMM",
        status=RecordStatus.REVIEW,
    )
    zap = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")

    result = corroborate.corroborate_against_zap([dirty], [zap])

    assert result[0].status == RecordStatus.ACCEPTED


def test_incoming_objects_not_mutated():
    """corroborate_against_zap never mutates the original CivicEvent objects."""
    dirty = _dirty_event(ulurp_number="C 240042 ZMM", project_thread_id="zap:P2024M0042")
    zap = _zap_event("zap:P2024M0042", ulurp_number="C 240042 ZMM")
    original_status = dirty.status

    corroborate.corroborate_against_zap([dirty], [zap])

    assert dirty.status == original_status  # original object untouched


# --------------------------------------------------------------------------- #
# Wiring: corroboration runs inside gather_live_events                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def offline_base(monkeypatch):
    """Silence live feeds; seed one ZAP event so corroboration has something to compare."""
    zap = CivicEvent(
        source_id="nyc_zap",
        source_record_id="P2024M0042",
        project_thread_id="zap:P2024M0042",
        ulurp_number="C 240042 ZMM",
    )
    # Two iter_feed calls (HPD, DOB) — both empty.
    feeds = iter([[], []])
    monkeypatch.setattr(harlem_digest, "iter_feed", lambda *a, **k: iter(next(feeds)))
    monkeypatch.setattr(harlem_digest, "iter_zap_events", lambda *a, **k: iter([zap]))
    monkeypatch.setattr(harlem_digest, "discover_cd_hearings", lambda *a, **k: [])
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: None)
    return zap


def test_corroborate_wired_into_gather_live_events(monkeypatch, offline_base):
    """End-to-end wiring of the dirty-vs-ZAP reconciliation inside gather_live_events.

    Two dirty ULURP-packet extractions, same project thread as the seeded ZAP record:
    one repeats the ULURP number ZAP already covers (a duplicate digest entry — dropped;
    ZAP is authoritative and carries the verified City record link), one carries no
    ULURP number (survives, upgraded to ACCEPTED because the ZAP thread corroborates it).
    """
    packet_ref = PacketRef(
        ulurp_number="C 240042 ZMM",
        url="https://a836-zap.nyc.gov/document/ulurp/C240042ZMM",
        project_thread_id="zap:P2024M0042",
        title="ULURP packet C 240042 ZMM",
    )
    duplicate_of_zap = CivicEvent(
        source_id="nyc_ulurp_packet",
        source_record_id="nyc_ulurp_packet-item-0000",
        project_thread_id="zap:P2024M0042",
        ulurp_number="C 240042 ZMM",
        status=RecordStatus.REVIEW,
    )
    detail_without_number = CivicEvent(
        source_id="nyc_ulurp_packet",
        source_record_id="nyc_ulurp_packet-item-0001",
        project_thread_id="zap:P2024M0042",
        ulurp_number=None,
        status=RecordStatus.REVIEW,
    )

    monkeypatch.setattr(
        harlem_digest.ulurp_packet, "discover_packets", lambda *a, **k: [packet_ref]
    )
    monkeypatch.setattr(harlem_digest.ulurp_packet, "fetch", lambda url: b"%PDF-1.7 fake")
    monkeypatch.setattr(
        harlem_digest.pdf_text, "extract_text", lambda b: ParsedDoc(text="packet text")
    )
    monkeypatch.setattr(
        harlem_digest.extractor,
        "extract",
        lambda doc, *, source_id: [duplicate_of_zap, detail_without_number],
    )

    events = harlem_digest.gather_live_events(include_ulurp_packet=True)

    packet_events = [e for e in events if e.source_id == "nyc_ulurp_packet"]
    assert len(packet_events) == 1
    assert packet_events[0].source_record_id == "nyc_ulurp_packet-item-0001"
    assert packet_events[0].status == RecordStatus.ACCEPTED
