"""Stage 1 (Fetch) — building energy letter grade enrichment via Socrata. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull NYC's Local Law 33 building energy-efficiency **letter grade**
(A-F) from NYC Open Data (Socrata dataset ``355w-xvp2``), map each graded building to the
canonical :class:`~ingest.extract.schemas.CivicEvent` shape, and surface it as low-key
*context on a building* — the legitimate, government-graded "is this building well-run"
proxy. The dataset is clean structured JSON, so this connector emits records directly and
SKIPS Parse and Extract entirely (Rule 1).

Resident value: "the building next door (or my own) scored a D/F on the City's energy grade"
— an official accountability signal, posted by law at the building's public entrance, that
threads onto the same BBL as its permits and violations (Rule 7 / Rule 15). It is *context*,
not an action item: there is no hearing to attend, so it never leads the "Act on this"
section — it rides along with the building it describes.

Why only D/F (the severity threshold): an A-C grade is good or average news and not
actionable; surfacing every graded building would firehose the digest (200 D/F buildings
already exist in East Harlem alone). We emit only the below-average grades, and the
enrichment is bounded to BBLs the digest already surfaces, so it can never flood the email.
The threshold is a tunable constant, mirroring the displacement-signal windows in
``dob_hpd``.

Shared machinery: the generic Socrata pull (:class:`~ingest.sources.nyc.dob_hpd.SocrataFeed`
+ :func:`~ingest.sources.nyc.dob_hpd.iter_feed`) is reused rather than re-rolled — an energy
grade is "just another scoped feed." Only the dataset id, the East Harlem scope, the mapper,
and the severity threshold are new here.

Rules honored
-------------
- Rule 1  (LLM only on dirty inputs): structured -> NO LLM, ever. The plain-English grade
          explanation is a deterministic template, not generation.
- Rule 4  (NYC-specific code in nyc/): the dataset id, the East Harlem BBL scope, and the
          Local Law 33 grade vocabulary are NYC knowledge and stay here.
- Rule 7  (project_thread_id + JSONB): the grade threads onto ``bbl:<BBL>`` so it groups with
          a building's permits/violations; per-source quirks go in ``extras``.
- Rule 10 (confidence routing): a posted City grade is a fact, not an inference ->
          ``confidence=1.0``, ``status=ACCEPTED``.
- Rule 15 (SoR key = (source_id, source_record_id) + BBL): the dataset is BBL-native, so
          ``source_record_id = bbl`` and the cross-source join key is free (no geocoding).

================================ DECISION RECORD ================================
Energy-grade enrichment scope (2026-06-13) — SPEC_NEXT_PHASE §B.2 ("official grades YES").

  WHAT: attach the Local Law 33 energy letter grade to East Harlem buildings as ACCEPTED,
        row-cited context — the ToS-clean, government-graded alternative to consumer reviews.
  DATASET (verified live against NYC Open Data, 2026-06-13):
    - 355w-xvp2 "DOB Sustainability Compliance Map: Local Law 33": 26k+ graded buildings;
      BBL-native (``bbl`` is a 10-digit numeric column), carries ``letterscore`` (A/B/C/D/F;
      NYC's scale skips E) and ``energy_star_score`` (1-100 percentile). 359 graded buildings
      in the East Harlem BBL range; 200 of them D/F.
  WHERE: Manhattan Community District 11 (East Harlem). The dataset has NO community_district
        or ZIP column, so it is scoped by the CD11 BBL range (boro 1 + block 1600-1820 ->
        1016000000..1018209999) — the dataset-supported approximation, analogous to HPD being
        scoped by ZIP because it lacks a CD column (a documented boundary approximation, not a
        bug). The cross-feed BBL join is exact per-building.
  SEVERITY: only D/F surface (POOR_ENERGY_GRADES). A-C are filtered out as non-actionable.
  ENRICHMENT, NOT A FIREHOSE: the runner pulls grades only for BBLs already surfaced by the
        HPD/DOB/ZAP feeds, so the volume is bounded by the building feed, not the dataset.
=================================================================================
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from ingest.extract.schemas import CivicEvent, RecordStatus
from ingest.observability import get_logger
from ingest.sources.nyc import citations
from ingest.sources.nyc.dob_hpd import SocrataFeed, iter_feed

log = get_logger(__name__)

SOURCE_ID_ENERGY = "nyc_energy_grade"

# Socrata dataset id (the verifiable row link is built from this — see citations.py).
DATASET_ENERGY = "355w-xvp2"  # DOB Sustainability Compliance Map: Local Law 33 letter grade

# East Harlem (Manhattan CD11) BBL range. The dataset has no community_district/ZIP column,
# so we bound by the CD11 tax-block range as a 10-digit BBL window (boro 1, block 1600-1820,
# any lot). 10-digit BBLs are zero-padded, so a lexical ``between`` is also a numeric one.
EAST_HARLEM_BBL_LO = "1016000000"
EAST_HARLEM_BBL_HI = "1018209999"

# Severity threshold (Rule-8 companion): only below-average grades are actionable context.
# A-C is good/average news and would firehose the digest; D/F is the "poorly-run building"
# signal. Tunable, NYC-specific — mirrors the displacement windows in dob_hpd.
POOR_ENERGY_GRADES = ("D", "F")
# NYC's Local Law 33 scale runs A (best) to F (worst); it skips E.
VALID_ENERGY_GRADES = ("A", "B", "C", "D", "F")


def _sql_in(values: tuple[str, ...]) -> str:
    """Render a SoQL ``IN`` list: ``('D', 'F')`` (values are trusted constants)."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _valid_bbl(raw: Any) -> str | None:
    """Return a well-formed 10-digit BBL string, else ``None`` (fail soft, not guess)."""
    value = str(raw or "").strip()
    return value if len(value) == 10 and value.isdigit() else None


def _grade_meaning(grade: str) -> str:
    """Plain-language, factually-honest gloss for a Local Law 33 letter grade (Rule 1).

    NYC requires covered buildings (>25,000 sq ft) to post this grade at their public
    entrance. D is below average; F is the lowest grade — typically posted when a building
    did not file the City's required energy benchmarking. We do not assert the exact ENERGY
    STAR cutoffs (NYC trivia we would have to verify); only the directionality (A best, F
    worst) and the posting requirement, both checkable.
    """
    if grade == "F":
        return (
            "the lowest grade, typically posted when a building did not file the City's "
            "required energy benchmarking"
        )
    return "below average (grades run A best to F)"


def _energy_grade_to_event(rec: Mapping[str, Any]) -> CivicEvent:
    """Map one ``355w-xvp2`` row to a canonical :class:`CivicEvent` (no LLM — Rule 1).

    Grade is *context on a building*, so the event carries no ``event_date`` or ``deadline``
    (there is nothing to act on by a date) — it rides along with the building it describes.
    """
    now = datetime.now(UTC)
    grade = (rec.get("letterscore") or "").strip().upper()
    record_bbl = _valid_bbl(rec.get("bbl"))
    addr = (rec.get("address") or "").strip().title() or None
    star = rec.get("energy_star_score")

    summary = (
        f"The City posted a Local Law 33 energy-efficiency grade of {grade} for "
        f"{addr or 'this building'}: {_grade_meaning(grade)}."
    )
    if star not in (None, "", "0"):
        summary += f" Its ENERGY STAR score is {star}/100."

    grade_citations = []
    if record_bbl:
        grade_citations.append(
            citations.socrata_row(
                DATASET_ENERGY,
                "bbl",
                record_bbl,
                label=f"Local Law 33 energy grade for BBL {record_bbl} (NYC Open Data)",
                retrieved_at=now,
            )
        )

    return CivicEvent(
        source_id=SOURCE_ID_ENERGY,
        # BBL is the dataset's natural key (one grade per building); it is also the
        # cross-source join key, so the SoR identity and the join key coincide (Rule 15).
        source_record_id=record_bbl or str(rec.get("bbl") or ""),
        bbl=record_bbl,
        project_thread_id=f"bbl:{record_bbl}" if record_bbl else None,  # Rule 7
        action_type="building_energy_grade",
        title=f"Building energy grade {grade} (Local Law 33)",
        summary=summary,
        address=addr,
        confidence=1.0,  # a posted City grade is a fact, not an inference (Rule 10)
        status=RecordStatus.ACCEPTED,
        citations=grade_citations,
        extras={
            "letter_grade": grade,
            "energy_star_score": star,
            "building_class": rec.get("building_class"),
            "gross_square_footage": rec.get("dof_gross_square_footage"),
            "building_count": rec.get("building_count"),
            "borough_name": rec.get("boroughname"),
        },
        extracted_at=now,
    )


ENERGY_GRADE_FEED = SocrataFeed(
    source_id=SOURCE_ID_ENERGY,
    dataset_id=DATASET_ENERGY,
    primary_key=("bbl",),
    mapper=_energy_grade_to_event,
    scope_where=f"bbl between '{EAST_HARLEM_BBL_LO}' and '{EAST_HARLEM_BBL_HI}'",
)


def discover_energy_grades(
    bbls: Iterable[str] | None = None,
    *,
    poor_only: bool = True,
    limit: int | None = None,
) -> Iterator[CivicEvent]:
    """Yield Local Law 33 energy-grade events for East Harlem buildings (Rule 1, no LLM).

    Args:
        bbls: Restrict to these BBLs — the enrichment path. When the runner passes the
            BBLs already surfaced by the HPD/DOB/ZAP feeds, the grade volume is bounded by
            the building feed, so it can never firehose the digest. ``None`` pulls the whole
            East Harlem scope (the standalone demo/eval path).
        poor_only: Emit only the below-average grades in :data:`POOR_ENERGY_GRADES` (the
            actionable severity band). ``False`` emits every grade (A-F).
        limit: Optional cap on records yielded (handy for demos/tests).

    Yields:
        One ACCEPTED :class:`CivicEvent` per graded building, keyed on BBL (Rule 15).
    """
    bbl_list = tuple(sorted({b for b in (bbls or ()) if _valid_bbl(b)}))
    if bbls is not None and not bbl_list:
        return  # nothing to enrich — avoid an unbounded scope pull

    clauses: list[str] = []
    if poor_only:
        clauses.append(f"letterscore in {_sql_in(POOR_ENERGY_GRADES)}")
    if bbl_list:
        clauses.append(f"bbl in {_sql_in(bbl_list)}")
    where = " AND ".join(clauses) or None

    yield from iter_feed(ENERGY_GRADE_FEED, where=where, limit=limit)


# --------------------------------------------------------------------------- #
# Runnable demo: `python -m ingest.sources.nyc.building_grades`                 #
# --------------------------------------------------------------------------- #
def _demo() -> None:
    # ASCII-only output so it prints on any console (Windows cp1252 included).
    print("\n=== East Harlem buildings with a poor (D/F) Local Law 33 energy grade (first 8) ===")
    for i, ev in enumerate(discover_energy_grades(limit=8)):
        if i >= 8:
            break
        grade = ev.extras.get("letter_grade")
        print(f"  [{grade}] {ev.address or 'n/a'}  |  BBL {ev.bbl or 'n/a'}")
        print(f"      {ev.summary}")
        for c in ev.citations:
            print(f"      verify: [{c.kind}] {c.label}: {c.url}")


if __name__ == "__main__":
    _demo()
