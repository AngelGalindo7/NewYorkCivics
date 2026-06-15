"""Contract tests for the severe-311 building-context enrichment connector.

Runs fully offline against hardcoded ``erm2-nwe9`` rows (no Socrata, no DB). Locks the
trust-critical properties: per-ticket source identity, the per-building aggregation, the
BBL thread, the honest "a complaint is a report, not a violation" framing, the citation
audit + dataset registration, the capped row-exact links, plain-language labels (no raw
bureaucratic strings), and that a 311 summary is context that never leads the digest.
"""

from __future__ import annotations

from datetime import date

from ingest.deliver.digest import _category_weight, build_digest
from ingest.deliver.match import match_subscriber
from ingest.extract.schemas import RecordStatus
from ingest.sources.nyc import citations as cit_mod
from ingest.sources.nyc.service_requests import (
    _MAX_CITATIONS,
    DATASET_311,
    SEVERE_COMPLAINT_TYPES,
    SOURCE_ID_311,
    _summarize_building,
    _ticket_to_event,
)


def _ticket(uk: str, ctype: str, created: str, *, bbl: str = "1016500030", **extra) -> dict:
    row = {
        "unique_key": uk,
        "bbl": bbl,
        "complaint_type": ctype,
        "status": "Open",
        "incident_address": "123 EAST 116 STREET",
        "created_date": created,
    }
    row.update(extra)
    return row


SAMPLE_TICKETS = [
    _ticket(
        "70001",
        "HEAT/HOT WATER",
        "2026-06-01T08:00:00.000",
        latitude="40.7969",
        longitude="-73.9410",
    ),
    _ticket("70002", "HEAT/HOT WATER", "2026-05-15T08:00:00.000"),
    _ticket("70003", "PLUMBING", "2026-05-28T08:00:00.000"),
]


def _summary(rows=SAMPLE_TICKETS):
    return _summarize_building("1016500030", [_ticket_to_event(r) for r in rows])


# --------------------------------------------------------------------------- #
# Per-ticket mapping (internal scaffolding)                                    #
# --------------------------------------------------------------------------- #


def test_ticket_maps_identity_and_row_exact_citation():
    ev = _ticket_to_event(SAMPLE_TICKETS[0])
    assert ev.source_id == SOURCE_ID_311 == "nyc_311_habitability"
    assert ev.source_record_id == "70001"
    assert ev.bbl == "1016500030"
    assert ev.event_date == date(2026, 6, 1)
    assert ev.latitude == 40.7969 and ev.longitude == -73.9410
    socrata = next(c for c in ev.citations if "/resource/" in c.url)
    assert "unique_key=70001" in socrata.url
    assert DATASET_311 in socrata.url
    assert socrata.verifies == "exact_record"
    assert cit_mod.audit_citation(socrata) is None


def test_ticket_without_unique_key_emits_no_citation():
    # Fail soft: a ticket missing its key carries no row-exact link rather than a broken one.
    ev = _ticket_to_event({**SAMPLE_TICKETS[0], "unique_key": ""})
    assert ev.citations == []


# --------------------------------------------------------------------------- #
# Per-building aggregation                                                     #
# --------------------------------------------------------------------------- #


def test_summary_aggregates_count_and_threads_on_bbl():
    s = _summary()
    assert s.action_type == "habitability_complaints"
    assert s.source_record_id == "1016500030"
    assert s.bbl == "1016500030"
    assert s.project_thread_id == "bbl:1016500030"
    assert s.extras["complaint_count"] == 3
    assert s.status == RecordStatus.ACCEPTED and s.confidence == 1.0


def test_summary_carries_no_actionable_date():
    s = _summary()
    # Context, not an action: no event_date or deadline, so it can never enter the lead -- not
    # even a complaint filed today. The most-recent date is shown in the summary/extras instead.
    assert s.event_date is None
    assert s.deadline is None
    assert s.extras["most_recent"] == "2026-06-01"
    assert "Most recent: 2026-06-01" in s.summary


def test_summary_is_honest_about_reports_vs_violations():
    s = _summary()
    assert "not a confirmed violation" in s.summary
    assert "3 severe 311 complaints" in s.summary


def test_summary_uses_plain_language_not_raw_codes():
    s = _summary()
    # The reader sees "heat/hot water", never the raw "HEAT/HOT WATER" bureaucratic string.
    assert "heat/hot water" in s.summary
    assert "HEAT/HOT WATER" not in s.summary
    assert "HEAT/HOT WATER" not in (s.title or "")
    assert "heat/hot water" in (s.title or "")  # top type leads the title


def test_summary_breakdown_orders_by_frequency():
    s = _summary()
    # Two heat tickets, one plumbing -> heat leads both the title and the breakdown.
    assert s.title.startswith("3 recent 311 complaints (heat/hot water)")
    assert s.extras["complaint_breakdown"]["HEAT/HOT WATER"] == 2


def test_summary_carries_coords_from_a_ticket():
    s = _summary()
    assert s.latitude == 40.7969 and s.longitude == -73.9410


# --------------------------------------------------------------------------- #
# Citations: capped, row-exact, audit-clean, registered                        #
# --------------------------------------------------------------------------- #


def test_summary_citations_are_capped_and_audit_clean():
    many = [
        _ticket(str(80000 + i), "HEAT/HOT WATER", f"2026-06-{i + 1:02d}T08:00:00.000")
        for i in range(9)
    ]
    s = _summarize_building("1016500030", [_ticket_to_event(r) for r in many])
    assert s.extras["complaint_count"] == 9
    assert len(s.citations) == _MAX_CITATIONS  # capped
    for c in s.citations:
        assert cit_mod.audit_citation(c) is None
        assert c.verifies == "exact_record"


def test_summary_caps_to_most_recent_tickets():
    many = [
        _ticket(str(80000 + i), "HEAT/HOT WATER", f"2026-06-{i + 1:02d}T08:00:00.000")
        for i in range(9)
    ]
    s = _summarize_building("1016500030", [_ticket_to_event(r) for r in many])
    # The newest ticket (2026-06-09, key 80008) must be among the kept citations.
    assert any("unique_key=80008" in c.url for c in s.citations)


def test_dataset_is_registered_for_the_audit():
    assert DATASET_311 in cit_mod.KNOWN_DATASETS


# --------------------------------------------------------------------------- #
# Allowlist + ranker weight                                                    #
# --------------------------------------------------------------------------- #


def test_allowlist_is_habitability_and_excludes_noise():
    assert "HEAT/HOT WATER" in SEVERE_COMPLAINT_TYPES
    assert "Lead" in SEVERE_COMPLAINT_TYPES
    assert "Noise - Residential" not in SEVERE_COMPLAINT_TYPES
    assert "Illegal Parking" not in SEVERE_COMPLAINT_TYPES


def test_category_weight_is_low_and_below_a_permit():
    assert _category_weight("habitability_complaints") == 0.35
    assert _category_weight("habitability_complaints") < _category_weight("permit")


# --------------------------------------------------------------------------- #
# Seam check: the summary flows through the city-agnostic deliver path          #
# --------------------------------------------------------------------------- #


def test_summary_flows_through_deliver_and_never_leads():
    subscriber = {"email": "n@example.com", "bbl": "1016500030", "community_district": "111"}
    matched = match_subscriber(subscriber, [_summary()])
    digest = build_digest(subscriber, matched, asof=date(2026, 6, 14))
    assert digest["item_count"] == 1
    # Context, not an action item: it must never appear in the forward-looking lead.
    assert all(it["action_type"] != "habitability_complaints" for it in digest["lead_items"])


def test_summary_never_leads_even_for_a_same_day_complaint():
    # Regression: a complaint filed on the run day must not float the context into the lead.
    today = date(2026, 6, 14)
    rows = [_ticket("91001", "HEAT/HOT WATER", f"{today.isoformat()}T08:00:00.000")]
    s = _summarize_building("1016500030", [_ticket_to_event(r) for r in rows])
    subscriber = {"email": "n@example.com", "bbl": "1016500030", "community_district": "111"}
    matched = match_subscriber(subscriber, [s])
    digest = build_digest(subscriber, matched, asof=today)
    assert all(it["action_type"] != "habitability_complaints" for it in digest["lead_items"])


# --------------------------------------------------------------------------- #
# The connector drops tickets it cannot verify (the citation gate)             #
# --------------------------------------------------------------------------- #


def test_discover_drops_uncited_tickets(monkeypatch):
    # An ACCEPTED summary auto-ships, so it must never be built from a ticket with no row-exact
    # link. A ticket whose unique_key was blank carries no citation and must be dropped; a
    # building whose only tickets are uncited must yield no summary at all.
    from ingest.sources.nyc import service_requests as sr

    cited = sr._ticket_to_event(_ticket("90001", "HEAT/HOT WATER", "2026-06-01T08:00:00.000"))
    uncited_same_bldg = sr._ticket_to_event(_ticket("", "PLUMBING", "2026-06-02T08:00:00.000"))
    uncited_other_bldg = sr._ticket_to_event(
        _ticket("", "HEAT/HOT WATER", "2026-06-03T08:00:00.000", bbl="1016510005")
    )
    monkeypatch.setattr(
        sr, "iter_feed", lambda feed, **kw: iter([cited, uncited_same_bldg, uncited_other_bldg])
    )

    out = list(sr.discover_service_requests(bbls=["1016500030", "1016510005"]))
    assert len(out) == 1  # the uncited-only building yields nothing
    s = out[0]
    assert s.bbl == "1016500030"
    assert s.extras["complaint_count"] == 1  # the uncited plumbing ticket was dropped
    assert s.citations and all(cit_mod.audit_citation(c) is None for c in s.citations)
