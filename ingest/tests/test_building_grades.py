"""Contract tests for the Local Law 33 energy-grade enrichment connector.

Runs fully offline against hardcoded ``355w-xvp2`` rows (no Socrata, no DB). Locks the
trust-critical properties: source identity + BBL-as-key, the BBL thread, full-confidence
ACCEPTED routing for a posted City grade, the citation audit, the D/F severity threshold,
graceful handling of a malformed BBL, and that an energy-grade event drops straight into the
city-agnostic deliver pipeline (the connector/core seam check).
"""

from __future__ import annotations

import pytest

from ingest.extract.schemas import RecordStatus
from ingest.sources.nyc import citations as cit_mod
from ingest.sources.nyc.building_grades import (
    DATASET_ENERGY,
    POOR_ENERGY_GRADES,
    SOURCE_ID_ENERGY,
    VALID_ENERGY_GRADES,
    _energy_grade_to_event,
    _valid_bbl,
)

# Representative East Harlem row; field names mirror the 355w-xvp2 schema confirmed live
# 2026-06-13. BBL is a real East Harlem parcel; grade is illustrative.
SAMPLE_GRADE_REC = {
    "bbl": "1016500030",
    "address": "123 EAST 116 STREET",
    "boroughname": "MANHATTAN",
    "building_class": "D1",
    "letterscore": "F",
    "energy_star_score": "12",
    "dof_gross_square_footage": "84000",
    "building_count": "1",
}


@pytest.fixture
def grade_event():
    return _energy_grade_to_event(SAMPLE_GRADE_REC)


# --------------------------------------------------------------------------- #
# Source identity + BBL is the key                                             #
# --------------------------------------------------------------------------- #


def test_source_id(grade_event):
    assert grade_event.source_id == SOURCE_ID_ENERGY == "nyc_energy_grade"


def test_bbl_is_the_source_record_id(grade_event):
    # The dataset is BBL-native, so the per-source identity and the cross-source join
    # key coincide — no geocoding needed (unlike CAMIS-keyed restaurant grades).
    assert grade_event.bbl == "1016500030"
    assert grade_event.source_record_id == "1016500030"


# --------------------------------------------------------------------------- #
# The energy grade threads onto the building                                   #
# --------------------------------------------------------------------------- #


def test_threads_on_bbl(grade_event):
    assert grade_event.project_thread_id == "bbl:1016500030"


def test_action_type_is_building_energy_grade(grade_event):
    assert grade_event.action_type == "building_energy_grade"


# --------------------------------------------------------------------------- #
# A posted City grade is a fact, not an inference                              #
# --------------------------------------------------------------------------- #


def test_grade_is_accepted_full_confidence(grade_event):
    assert grade_event.status == RecordStatus.ACCEPTED
    assert grade_event.confidence == 1.0


def test_grade_is_context_not_an_action_item(grade_event):
    # No date to act on: an energy grade must never float into the "Act on this" lead.
    assert grade_event.event_date is None
    assert grade_event.deadline is None


# --------------------------------------------------------------------------- #
# Plain-English summary (deterministic, honest, no over-claim)                 #
# --------------------------------------------------------------------------- #


def test_summary_names_the_law_grade_and_score(grade_event):
    s = grade_event.summary
    assert "Local Law 33" in s
    assert "grade of F" in s
    # The summary says "this building" — the digest groups the record under a building
    # header, and one tax lot can span several street addresses, so repeating the
    # dataset's own address here can contradict the header. The address still travels
    # on the event's address field (asserted separately).
    assert "for this building" in s
    assert grade_event.address == "123 East 116 Street"
    assert "12/100" in s  # ENERGY STAR score surfaced


def test_summary_omits_a_blank_energy_star_score():
    rec = {**SAMPLE_GRADE_REC, "energy_star_score": "0"}
    ev = _energy_grade_to_event(rec)
    assert "/100" not in ev.summary  # a 0/missing score is not a meaningful number


def test_extras_carry_the_grade_fields(grade_event):
    assert grade_event.extras["letter_grade"] == "F"
    assert grade_event.extras["energy_star_score"] == "12"
    assert grade_event.extras["building_class"] == "D1"


# --------------------------------------------------------------------------- #
# Every emitted citation passes the structural audit                           #
# --------------------------------------------------------------------------- #


def test_citation_is_row_exact_and_audits_clean(grade_event):
    assert grade_event.citations, "a graded building must carry a row-exact citation"
    for c in grade_event.citations:
        assert cit_mod.audit_citation(c) is None, f"citation failed audit: {c.url}"
    data_links = [c for c in grade_event.citations if c.kind == "data_source"]
    assert data_links and all(c.verifies == "exact_record" for c in data_links)
    assert DATASET_ENERGY in data_links[0].url


def test_dataset_is_registered_for_the_audit():
    # The audit rejects an unregistered Socrata dataset; the connector's dataset must be known.
    assert DATASET_ENERGY in cit_mod.KNOWN_DATASETS


# --------------------------------------------------------------------------- #
# Severity threshold + fail-soft parsing                                       #
# --------------------------------------------------------------------------- #


def test_poor_grades_are_d_and_f():
    assert POOR_ENERGY_GRADES == ("D", "F")
    assert set(POOR_ENERGY_GRADES).issubset(set(VALID_ENERGY_GRADES))


def test_nyc_grade_scale_skips_e():
    assert "E" not in VALID_ENERGY_GRADES


def test_valid_bbl_rejects_malformed():
    assert _valid_bbl("1016500030") == "1016500030"
    assert _valid_bbl("16500030") is None  # too short
    assert _valid_bbl("10165000ab") is None  # non-numeric
    assert _valid_bbl(None) is None
    assert _valid_bbl("") is None


def test_malformed_bbl_yields_no_citation_no_thread():
    # Fail soft: a row with an unusable BBL still maps, but carries no row-exact link
    # (nothing to verify against) and no BBL thread — never a guessed/404 citation.
    rec = {**SAMPLE_GRADE_REC, "bbl": "bad"}
    ev = _energy_grade_to_event(rec)
    assert ev.bbl is None
    assert ev.project_thread_id is None
    assert ev.citations == []


# --------------------------------------------------------------------------- #
# Seam check: the event flows through the city-agnostic deliver path           #
# --------------------------------------------------------------------------- #


def test_event_flows_through_deliver(grade_event):
    from datetime import date

    from ingest.deliver.digest import build_digest
    from ingest.deliver.match import match_subscriber

    subscriber = {"email": "n@example.com", "bbl": "1016500030", "community_district": "111"}
    matched = match_subscriber(subscriber, [grade_event])
    digest = build_digest(subscriber, matched, asof=date(2026, 6, 13))
    # Same BBL as the subscriber -> on_your_block; it renders as a verified context item,
    # never in the "Act on this" lead (no actionable date).
    assert digest["item_count"] == 1
    assert all(it["action_type"] != "building_energy_grade" for it in digest["lead_items"])


def test_category_weight_ranks_grade_below_a_permit():
    from ingest.deliver.digest import _category_weight

    assert _category_weight("building_energy_grade") == 0.3
    assert _category_weight("building_energy_grade") < _category_weight("permit")


# --------------------------------------------------------------------------- #
# Runner enrichment pass: coordinate backfill so a grade threads to its building #
# --------------------------------------------------------------------------- #


def test_enrichment_backfills_coords_for_threading(monkeypatch):
    # A grade row carries no coordinates of its own. The runner must stamp the lat/lng of
    # a co-located surfaced event on the same building, so the grade lands in the same
    # proximity band and groups with that building's permits/violations instead of being
    # banded to the catch-all "in your area".
    from ingest.extract.schemas import CivicEvent
    from ingest.sources.nyc import harlem_digest

    surfaced = CivicEvent(
        source_id="nyc_dob_now",
        source_record_id="P1",
        bbl="1016500030",
        latitude=40.7969,
        longitude=-73.9410,
        status=RecordStatus.ACCEPTED,
    )
    grade = _energy_grade_to_event(SAMPLE_GRADE_REC)  # same BBL, no coordinates
    assert grade.latitude is None and grade.longitude is None

    monkeypatch.setattr(harlem_digest, "discover_energy_grades", lambda bbls: iter([grade]))
    enriched = harlem_digest._enrich_with_energy_grades([surfaced])

    assert len(enriched) == 1
    assert enriched[0].bbl == "1016500030"
    assert enriched[0].latitude == 40.7969  # carried over from the surfaced event
    assert enriched[0].longitude == -73.9410
