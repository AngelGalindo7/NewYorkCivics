"""Offline tests for _link_zap_to_legistar — cross-source ULURP->Legistar join.

All tests run fully offline: find_matter_by_ulurp and _get_all are monkeypatched.
"""

from __future__ import annotations

from datetime import date

from ingest.extract.schemas import CivicEvent
from ingest.sources.nyc import harlem_digest
from ingest.sources.nyc.harlem_digest import _link_zap_to_legistar


def _zap_event(
    ulurp_number: str = "C 240042 ZMM",
    project_id: str = "P2024M0042",
    cpc_stage: str | None = None,
    deadline: date | None = None,
) -> CivicEvent:
    return CivicEvent(
        source_id="nyc_zap",
        source_record_id=project_id,
        project_thread_id=f"zap:{project_id}",
        ulurp_number=ulurp_number,
        deadline=deadline,
        extras={"project_id": project_id, "cpc_stage": cpc_stage},
    )


SAMPLE_MATTER = {
    "MatterId": 9999,
    "MatterTitle": "C 240042 ZMM - East Harlem Rezoning",
    "MatterStatusName": "Adopted",
    "MatterTypeId": 2,
}


def test_link_enriches_project_thread_id(monkeypatch):
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: SAMPLE_MATTER)
    ev = _zap_event()
    [enriched] = _link_zap_to_legistar([ev])
    assert enriched.project_thread_id == "zap:P2024M0042|legistar:matter:9999"


def test_link_leaves_non_zap_events_unchanged(monkeypatch):
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: SAMPLE_MATTER)
    non_zap = CivicEvent(
        source_id="nyc_legistar",
        source_record_id="event:100",
        project_thread_id="legistar:event:100",
    )
    [result] = _link_zap_to_legistar([non_zap])
    assert result.project_thread_id == "legistar:event:100"


def test_link_leaves_zap_without_ulurp_unchanged(monkeypatch):
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: SAMPLE_MATTER)
    ev = CivicEvent(
        source_id="nyc_zap",
        source_record_id="P2024M0099",
        project_thread_id="zap:P2024M0099",
        ulurp_number=None,
        extras={"project_id": "P2024M0099"},
    )
    [result] = _link_zap_to_legistar([ev])
    assert result.project_thread_id == "zap:P2024M0099"


def test_link_keeps_original_when_no_match(monkeypatch):
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: None)
    ev = _zap_event()
    [result] = _link_zap_to_legistar([ev])
    assert result.project_thread_id == "zap:P2024M0042"


def test_link_fails_soft_on_lookup_error(monkeypatch):
    def _boom(ulurp):
        raise RuntimeError("httpx not available")

    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", _boom)
    ev = _zap_event()
    [result] = _link_zap_to_legistar([ev])
    assert result.project_thread_id == "zap:P2024M0042"


def test_link_overrides_cpc_deadline_with_legistar_hearing(monkeypatch):
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: SAMPLE_MATTER)

    hearing_history = [
        {
            "MatterHistoryActionName": "Hearing",
            "MatterHistoryActionDate": "2026-07-15T00:00:00",
            "MatterHistoryEventItemId": 555,
        }
    ]

    import ingest.sources.nyc.legistar as _leg

    monkeypatch.setattr(_leg, "_get_all", lambda path, **kw: hearing_history)

    ev = _zap_event(cpc_stage="cpc_review", deadline=date(2026, 6, 30))
    [result] = _link_zap_to_legistar([ev])
    assert result.deadline == date(2026, 7, 15)
    assert result.extras.get("cpc_stage") == "cpc_hearing_scheduled"


def test_link_keeps_approximated_deadline_when_no_hearing_history(monkeypatch):
    monkeypatch.setattr(harlem_digest, "find_matter_by_ulurp", lambda ulurp: SAMPLE_MATTER)

    import ingest.sources.nyc.legistar as _leg

    monkeypatch.setattr(_leg, "_get_all", lambda path, **kw: [])

    ev = _zap_event(cpc_stage="cpc_review", deadline=date(2026, 6, 30))
    [result] = _link_zap_to_legistar([ev])
    assert result.deadline == date(2026, 6, 30)
