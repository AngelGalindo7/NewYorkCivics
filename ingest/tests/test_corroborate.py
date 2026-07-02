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


def test_structured_source_with_matching_ulurp_survives_dedup(monkeypatch, offline_base):
    """The ZAP-authoritative dedup is scoped to dirty sources: a structured feed
    (e.g. Legistar) carrying the same ULURP number must never be dropped."""
    legistar_event = CivicEvent(
        source_id="nyc_legistar",
        source_record_id="matter:9999",
        project_thread_id="legistar:matter:9999",
        ulurp_number="C 240042 ZMM",
    )
    monkeypatch.setattr(harlem_digest, "discover_cd_hearings", lambda *a, **k: [legistar_event])

    events = harlem_digest.gather_live_events()

    assert any(e.source_id == "nyc_legistar" for e in events)


# --------------------------------------------------------------------------- #
# normalize_address                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("58-62 East 125th Street", "58-62 east 125 street"),
        ("58-62 East 125th Street", "58-62 East 125 Street"),
        ("58-62 East 125th Street", "58-62 East 125th St."),
        ("2253 Third Avenue", "2253 third ave"),
        ("308 East 116th Street", "308 East 116th Street,"),
    ],
)
def test_normalize_address_equivalences(left, right):
    assert corroborate.normalize_address(left) == corroborate.normalize_address(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # A single house number is not the range containing it.
        ("58 East 125th Street", "58-62 East 125th Street"),
        # Compass words are deliberately NOT aliased — conservative equality.
        ("58-62 East 125th Street", "58-62 E 125th Street"),
        ("100 East 116th Street", "100 West 116th Street"),
    ],
)
def test_normalize_address_non_equivalences(left, right):
    assert corroborate.normalize_address(left) != corroborate.normalize_address(right)


# --------------------------------------------------------------------------- #
# thread_dirty_by_address                                                      #
# --------------------------------------------------------------------------- #


def _cb_rezoning(
    rid: str = "cb-item-1",
    address: str | None = "58-62 East 125th Street",
    action_type: str = "rezoning",
    ulurp_number: str | None = None,
    deadline=None,
) -> CivicEvent:
    return CivicEvent(
        source_id="nyc_cb_mn11",
        source_record_id=rid,
        action_type=action_type,
        title="58-62 East 125th Street Rezoning",
        address=address,
        ulurp_number=ulurp_number,
        deadline=deadline,
        status=RecordStatus.REVIEW,
    )


def _zap_with_address(
    address: str = "58-62 East 125 Street",
    project_thread_id: str = "zap:2020M0383",
    source_record_id: str = "2020M0383",
    ulurp_number: str | None = "210495ZMM",
    deadline=None,
    extras: dict | None = None,
) -> CivicEvent:
    return CivicEvent(
        source_id="nyc_zap",
        source_record_id=source_record_id,
        project_thread_id=project_thread_id,
        address=address,
        ulurp_number=ulurp_number,
        deadline=deadline,
        extras=extras or {},
    )


def test_thread_by_address_single_match_threads_and_marks():
    dirty = _cb_rezoning()
    zap = _zap_with_address()
    [threaded] = corroborate.thread_dirty_by_address([dirty], [zap])
    assert threaded.project_thread_id == "zap:2020M0383"
    assert threaded.extras.get("address_threaded") is True
    assert dirty.project_thread_id is None  # original untouched


def test_thread_by_address_ambiguous_leaves_unchanged():
    dirty = _cb_rezoning()
    zap_a = _zap_with_address(project_thread_id="zap:A", source_record_id="A")
    zap_b = _zap_with_address(project_thread_id="zap:B", source_record_id="B")
    [result] = corroborate.thread_dirty_by_address([dirty], [zap_a, zap_b])
    assert result.project_thread_id is None
    assert "address_threaded" not in result.extras


def test_thread_by_address_no_match_or_wrong_shape_unchanged():
    zap = _zap_with_address(address="1 Other Place")
    for ev in (
        _cb_rezoning(),  # address doesn't match any ZAP record
        _cb_rezoning(address=None),  # no address to join on
        _cb_rezoning(ulurp_number="C 999999 ZMM"),  # has a ULURP -> ULURP path owns it
        _cb_rezoning(action_type="street_event"),  # not a land-use action
    ):
        [result] = corroborate.thread_dirty_by_address([ev], [zap])
        assert result.project_thread_id == ev.project_thread_id
        assert "address_threaded" not in result.extras


def test_thread_by_address_ignores_structured_sources():
    structured = CivicEvent(
        source_id="nyc_legistar",
        source_record_id="matter:1",
        action_type="rezoning",
        address="58-62 East 125th Street",
    )
    [result] = corroborate.thread_dirty_by_address([structured], [_zap_with_address()])
    assert result.project_thread_id is None


# --------------------------------------------------------------------------- #
# dedup_dirty_against_zap — merge-before-drop                                  #
# --------------------------------------------------------------------------- #


def test_dedup_drops_ulurp_duplicate_and_transfers_date_when_zap_has_none():
    from datetime import date

    dirty = _cb_rezoning(ulurp_number="210495ZMM", deadline=date(2026, 7, 14))
    zap = _zap_with_address(deadline=None)
    result = corroborate.dedup_dirty_against_zap([dirty, zap])

    assert [e.source_id for e in result] == ["nyc_zap"]
    note = result[0].extras.get("unverified_date_note")
    assert note and "2026-07-14" in note
    assert result[0].deadline is None  # the authoritative field is never overwritten
    assert zap.extras.get("unverified_date_note") is None  # original untouched


def test_dedup_transfers_date_over_approximated_window_only():
    from datetime import date

    # ZAP deadline is only the approximated 60-day window -> the CB date is worth noting.
    approx = _zap_with_address(deadline=date(2026, 6, 30), extras={"cpc_stage": "cpc_review"})
    dirty = _cb_rezoning(ulurp_number="210495ZMM", deadline=date(2026, 7, 14))
    result = corroborate.dedup_dirty_against_zap([dirty, approx])
    assert "2026-07-14" in (result[0].extras.get("unverified_date_note") or "")
    assert result[0].deadline == date(2026, 6, 30)

    # ZAP deadline is confirmed (no approximation flag) -> no note, just the drop.
    confirmed = _zap_with_address(deadline=date(2026, 6, 30))
    dirty2 = _cb_rezoning(ulurp_number="210495ZMM", deadline=date(2026, 7, 14))
    result2 = corroborate.dedup_dirty_against_zap([dirty2, confirmed])
    assert [e.source_id for e in result2] == ["nyc_zap"]
    assert result2[0].extras.get("unverified_date_note") is None


def test_dedup_multiple_duplicates_contribute_soonest_date_once():
    from datetime import date

    zap = _zap_with_address(deadline=None)
    later = _cb_rezoning(rid="cb-1", ulurp_number="210495ZMM", deadline=date(2026, 7, 20))
    sooner = _cb_rezoning(rid="cb-2", ulurp_number="210495ZMM", deadline=date(2026, 7, 14))
    result = corroborate.dedup_dirty_against_zap([later, sooner, zap])
    notes = [e.extras.get("unverified_date_note") for e in result]
    assert len(result) == 1
    assert "2026-07-14" in notes[0]
    assert "2026-07-20" not in notes[0]


def test_dedup_drops_address_threaded_duplicate():
    dirty = _cb_rezoning()
    zap = _zap_with_address()
    threaded = corroborate.thread_dirty_by_address([dirty], [zap])
    result = corroborate.dedup_dirty_against_zap(threaded + [zap])
    assert [e.source_id for e in result] == ["nyc_zap"]


def test_dedup_keeps_unthreaded_dirty_detail():
    # A packet detail threaded at discovery time (no address_threaded marker, no ULURP
    # duplicate) is enrichment, not a duplicate — it must survive.
    detail = CivicEvent(
        source_id="nyc_ulurp_packet",
        source_record_id="packet-detail-1",
        project_thread_id="zap:2020M0383",
        ulurp_number=None,
    )
    zap = _zap_with_address()
    result = corroborate.dedup_dirty_against_zap([detail, zap])
    assert {e.source_record_id for e in result} == {"packet-detail-1", "2020M0383"}


# --------------------------------------------------------------------------- #
# Wiring: the address-threaded CB duplicate merges into ZAP end to end         #
# --------------------------------------------------------------------------- #


def test_cb_duplicate_without_ulurp_merges_into_zap_end_to_end(monkeypatch):
    from datetime import date

    zap = _zap_with_address(
        address="58-62 East 125 Street",
        deadline=date(2026, 6, 30),
        extras={"cpc_stage": "cpc_review", "project_id": "2020M0383"},
    )
    feeds = iter([[], []])
    monkeypatch.setattr(harlem_digest, "iter_feed", lambda *a, **k: iter(next(feeds)))
    monkeypatch.setattr(harlem_digest, "iter_zap_events", lambda *a, **k: iter([zap]))
    monkeypatch.setattr(harlem_digest, "discover_cd_hearings", lambda *a, **k: [])
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: None)

    from ingest.sources.nyc.cb_agenda import AgendaRef

    cb_extraction = _cb_rezoning(address="58-62 East 125th Street", deadline=date(2026, 7, 14))
    agenda_ref = AgendaRef(
        board="MN11", url="https://x/agenda.pdf", meeting_date=None, title="CB11 agenda"
    )
    monkeypatch.setattr(harlem_digest.cb_agenda, "discover_agendas", lambda *a, **k: [agenda_ref])
    monkeypatch.setattr(harlem_digest.cb_agenda, "fetch", lambda url: b"%PDF-1.7 fake")
    monkeypatch.setattr(
        harlem_digest.pdf_text, "extract_text", lambda b: ParsedDoc(text="agenda text")
    )
    monkeypatch.setattr(
        harlem_digest.extractor, "extract", lambda doc, *, source_id: [cb_extraction]
    )

    events = harlem_digest.gather_live_events(include_cb_agenda=True)

    project_events = [e for e in events if "125" in (e.address or "")]
    assert len(project_events) == 1
    survivor = project_events[0]
    assert survivor.source_id == "nyc_zap"
    assert "2026-07-14" in (survivor.extras.get("unverified_date_note") or "")
    assert survivor.deadline == date(2026, 6, 30)
