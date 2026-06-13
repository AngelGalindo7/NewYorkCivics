"""Contract tests for the DOB permit mapper's building identity + verify links.

Runs fully offline against a hard-coded DOB permit row (no Socrata, no DB). Locks the
trust-critical behavior a live run surfaced: a permit must identify its building by the
row's canonical BIN/BBL, not a development lot — otherwise the "Verify" link lands on a
profile that shows "no permits" for a real permit, and the building never threads with
its same-site neighbors.

The fixture mirrors a real East Harlem row (Sendero Verde Phase 2, permit 3986011),
whose block/lot (the development lot 0020) deliberately disagrees with its own ``bbl``
field (the condo billing lot 7502) and ``bin__`` (the building, 1091648).
"""

from __future__ import annotations

from ingest.sources.nyc.citations import audit_citation, bis_property, dob_permits_by_bin
from ingest.sources.nyc.dob_hpd import _dob_permit_to_event, _record_bbl

SAMPLE_DOB_PERMIT = {
    "permit_si_no": "3986011",
    "job_type": "NB",
    "house__": "60",
    "street_name": "EAST 112TH STREET",
    "borough": "MANHATTAN",
    "block": "01617",
    "lot": "00020",
    "bin__": "1091648",
    "bbl": "1016177502",
    "issuance_date": "02/10/2025",
    "owner_s_business_name": "SV-B OWNERS LLC",
    "gis_latitude": "40.796994",
    "gis_longitude": "-73.945655",
}


def _building_link(event):
    return next(c for c in event.citations if "a810-bisweb.nyc.gov" in c.url)


# ── building link: prefer the permits-by-BIN list ─────────────────────────────


def test_permit_building_link_uses_permits_by_bin():
    link = _building_link(_dob_permit_to_event(SAMPLE_DOB_PERMIT))
    assert "PermitsInProcessIssuedByBinServlet" in link.url
    assert "allbin=1091648" in link.url  # the permit's own building BIN, not the condo lot
    assert link.verifies == "exact_building"
    assert audit_citation(link) is None  # passes the structural citation guard


def test_permit_building_link_falls_back_to_property_profile_without_bin():
    rec = {k: v for k, v in SAMPLE_DOB_PERMIT.items() if k != "bin__"}
    link = _building_link(_dob_permit_to_event(rec))
    assert "PropertyProfileOverviewServlet" in link.url
    assert "block=1617" in link.url and "lot=20" in link.url


def test_dob_permits_by_bin_builds_allbin_url():
    c = dob_permits_by_bin("1091648")
    assert c is not None
    assert (
        c.url
        == "https://a810-bisweb.nyc.gov/bisweb/PermitsInProcessIssuedByBinServlet?requestid=0&allbin=1091648"
    )
    assert audit_citation(c) is None


def test_dob_permits_by_bin_none_without_bin():
    assert dob_permits_by_bin("") is None
    assert dob_permits_by_bin(None) is None


def test_bis_property_returns_none_without_block_lot():
    assert bis_property(None, None, None) is None


# ── event BBL: prefer the row's own canonical value ───────────────────────────


def test_permit_bbl_prefers_record_field():
    # The row's own bbl (condo billing lot 7502) wins over deriving lot 0020 from block/lot,
    # so the permit threads with same-site events instead of an orphan development lot.
    assert _dob_permit_to_event(SAMPLE_DOB_PERMIT).bbl == "1016177502"


def test_permit_bbl_falls_back_to_derivation_without_bbl_field():
    rec = {k: v for k, v in SAMPLE_DOB_PERMIT.items() if k != "bbl"}
    assert _dob_permit_to_event(rec).bbl == "1016170020"  # boro 1 + block 01617 + lot 0020


def test_record_bbl_validation():
    assert _record_bbl("1016177502") == "1016177502"
    assert _record_bbl(None) is None
    assert _record_bbl("") is None
    assert _record_bbl("123") is None  # too short
    assert _record_bbl("10161775XX") is None  # non-numeric


# ── the row-exact Socrata link is still the hard proof ────────────────────────


def test_permit_keeps_row_exact_socrata_link():
    socrata = next(
        c for c in _dob_permit_to_event(SAMPLE_DOB_PERMIT).citations if "/resource/" in c.url
    )
    assert "permit_si_no=3986011" in socrata.url
    assert socrata.verifies == "exact_record"
    assert audit_citation(socrata) is None
