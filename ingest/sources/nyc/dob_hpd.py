"""Stage 1 (Fetch) — DOB permits + HPD violations via Socrata. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull NYC DOB building permits and HPD code violations from
NYC Open Data (Socrata / SODA) as clean structured data, map them to the canonical
:class:`~ingest.extract.schemas.CivicEvent` shape, and expose the cross-feed
displacement signal. Both feeds are structured JSON, so this connector emits records
directly and SKIPS Parse and Extract entirely (Rule 1).

Resident value: "what's on my building" (HPD violations) and "what's being built
near me" (DOB permits, filter NB / A1 / DM). These are the Phase 1 first sources.

Rules honored
-------------
- Rule 1 (LLM only on dirty inputs): structured -> NO LLM, ever. Plain-English
  summaries here are deterministic templates, not generation.
- Rule 4 (NYC-specific code in nyc/): dataset ids, the East Harlem boundary, BBL
  construction, and displacement thresholds are NYC knowledge and stay here.
- Rule 7 (project_thread_id + JSONB): the displacement signal threads a building's
  story on ``bbl:<BBL>``; per-source quirks go in ``extras`` (JSONB).
- Rule 10 (confidence routing): structured records are trusted -> ACCEPTED; the
  displacement signal is a correlation, not a fact -> REVIEW (never auto-shipped).
- Rule 15 (SoR key = (source_id, source_record_id) + BBL): per-record identity;
  BBL is the join key the displacement signal correlates on.

================================ DECISION RECORD ================================
v0 PROTOTYPE SCOPE (2026-05-31) — full reasoning in the project decision log (kept locally).

  WHAT: one neighborhood, structured feeds only, no LLM, no DB required to run.
  WHERE: East Harlem = Manhattan Community District 11.
  DATA (verified live against NYC Open Data, 2026-05-31):
    - HPD violations  wvxf-dwi5 : 180,378 in ZIPs 10029/10035; 5,105 Class C
                                  since 2025-06 -> the signal trigger is abundant.
    - DOB permits     ipu4-2q9a : 9,463 A1/NB/DM permits; carries community_board
                                  ='111' and gis_lat/lng (coords free, no GeoSupport).
  DIFFERENTIATORS (why this isn't just an open-data mirror):
    1. Displacement signal — Class C violation (90d) AND A1/NB/DM permit (180d) on
       the SAME BBL. Nobody hands a resident this cross-feed correlation.
    2. ULURP land-use applications via ZAP (hgx4-8ukb) — next connector; the
       formal-rezoning differentiator, sourced clean (no PDF extraction yet).
  BOUNDARY ASYMMETRY (documented, not a bug): HPD has no community-district column
    -> scoped by ZIP (10029/10035); DOB has community_board -> scoped by '111'.
    Both approximate East Harlem; the displacement JOIN on BBL is exact per-building.
  DATA-QUALITY NOTE: DOB issuance_date is a MM/DD/YYYY *string* (not range-queryable
    in SoQL), and ``dobrundate`` marks REPROCESSING (renewals/status changes), not
    issuance — many 2010 permits carry a 2026 dobrundate. So the signal bounds volume
    server-side by ``dobrundate`` but filters to a TRUE 180-day window by parsing
    ``issuance_date`` in Python; otherwise reprocessed-old permits inflate the signal.
=================================================================================
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ingest.config import get_settings
from ingest.extract.schemas import Citation, CivicEvent, RecordStatus
from ingest.observability import get_logger
from ingest.sources.nyc import citations

if TYPE_CHECKING:
    from sodapy import Socrata

# Import-safety (test_smoke): only pydantic is an import-time dependency. The HTTP + Socrata +
# retry stack is needed only at fetch time, so guard it — the module must import with these deps
# absent (CI installs a minimal env). Real runs install requests/sodapy/tenacity.
try:
    from requests.exceptions import RequestException
    from sodapy import Socrata
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
except ImportError:
    RequestException = Exception

    def retry(*args: object, **kwargs: object):
        def _decorator(func):
            return func

        return _decorator

    def retry_if_exception_type(*args: object, **kwargs: object) -> None:
        return None

    def stop_after_attempt(*args: object, **kwargs: object) -> None:
        return None

    def wait_exponential(*args: object, **kwargs: object) -> None:
        return None


log = get_logger(__name__)

SOURCE_ID_DOB = "nyc_dob_now"
SOURCE_ID_HPD = "nyc_hpd_violations"
SOURCE_ID_DISPLACEMENT = "nyc_displacement_signal"

# Socrata dataset ids (the verifiable row link is built from these — see citations.py).
DATASET_HPD = "wvxf-dwi5"  # HPD housing-maintenance violations
DATASET_DOB = "ipu4-2q9a"  # DOB permit issuance

SOCRATA_DOMAIN = "data.cityofnewyork.us"
_PAGE = 1000  # Socrata default cap per request; we paginate by offset.
_TIMEOUT = 60  # seconds; NYC Open Data can be slow on sorted scans.


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(RequestException),
)
def _get_page(
    client: Socrata,
    dataset_id: str,
    *,
    where: str,
    limit: int,
    offset: int,
    order: str,
) -> list[dict[str, Any]]:
    """One Socrata page fetch with retry/backoff on transient HTTP errors (Rule 2)."""
    return client.get(dataset_id, where=where, limit=limit, offset=offset, order=order)


# --- East Harlem (Manhattan Community District 11) prototype boundary (Rule 4) ---
EAST_HARLEM_ZIPS = ("10029", "10035")  # HPD has no community-district column.
EAST_HARLEM_CB = "111"  # DOB community_board (boro 1 + board 11).
MANHATTAN_BORO_DIGIT = "1"

# --- Displacement signal — NYC-SPECIFIC, tunable thresholds ---
# A correlation flag, NOT a fact: a Class C (immediately hazardous) HPD violation in
# the last 90 days AND a major-work permit in the last 180 days on the SAME BBL.
DISPLACEMENT_VIOLATION_CLASS = "C"
DISPLACEMENT_PERMIT_JOB_TYPES = ("A1", "NB", "DM")
DISPLACEMENT_VIOLATION_WINDOW_DAYS = 90
DISPLACEMENT_PERMIT_WINDOW_DAYS = 180

# Plain-English labels for DOB job types (deterministic; NOT an LLM — Rule 1).
_JOB_TYPE_LABEL = {
    "A1": "major alteration",
    "A2": "minor alteration",
    "A3": "minor alteration",
    "NB": "new building",
    "DM": "demolition",
}
_BORO_NAME_TO_DIGIT = {
    "MANHATTAN": "1",
    "BRONX": "2",
    "BROOKLYN": "3",
    "QUEENS": "4",
    "STATEN ISLAND": "5",
}


# --------------------------------------------------------------------------- #
# Small deterministic helpers (NYC identity + date parsing)                    #
# --------------------------------------------------------------------------- #
def _sql_in(values: tuple[str, ...]) -> str:
    """Render a SoQL ``IN`` list: ``('10029', '10035')`` (values are trusted constants)."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def bbl(boro_digit: str | None, block: str | None, lot: str | None) -> str | None:
    """Construct a 10-char Borough-Block-Lot id: 1 boro digit + 5 block + 4 lot (Rule 15).

    Returns ``None`` if any part is missing or non-numeric (fail soft, not guess).
    """
    if not (boro_digit and block and lot):
        return None
    try:
        return f"{boro_digit}{int(block):05d}{int(lot):04d}"
    except (TypeError, ValueError):
        return None


def _record_bbl(raw: str | None) -> str | None:
    """A DOB row's own BBL if it is a well-formed 10-digit id, else ``None``.

    DOB rows carry a ``bbl`` field that already resolves a condo to its building/billing
    lot; preferring it over re-deriving from block/lot avoids pointing at a development
    lot that no resident reaches by searching the address.
    """
    value = str(raw or "").strip()
    return value if len(value) == 10 and value.isdigit() else None


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "")).date()
    except ValueError:
        return None


def _parse_mdy(value: str | None) -> date | None:
    """Parse DOB's ``MM/DD/YYYY`` date strings; ``None`` if blank/malformed."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _address(*parts: str | None) -> str | None:
    joined = " ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


# --------------------------------------------------------------------------- #
# Record -> CivicEvent mappers (one per feed)                                  #
# --------------------------------------------------------------------------- #
def _hpd_violation_to_event(rec: Mapping[str, Any]) -> CivicEvent:
    now = datetime.now(UTC)
    vclass = rec.get("class")
    addr = _address(rec.get("housenumber"), rec.get("streetname"))
    violation_id = str(rec["violationid"])
    severity = (
        "immediately hazardous"
        if vclass == "C"
        else "hazardous"
        if vclass == "B"
        else "non-hazardous"
    )
    return CivicEvent(
        source_id=SOURCE_ID_HPD,
        source_record_id=violation_id,
        bbl=bbl(rec.get("boroid"), rec.get("block"), rec.get("lot")),
        action_type="violation",
        title=f"HPD Class {vclass} violation" if vclass else "HPD violation",
        summary=(
            f"HPD cited {addr or 'this building'} for a Class {vclass} "
            f"({severity}) housing-maintenance violation."
        ),
        address=addr,
        event_date=_parse_iso(rec.get("inspectiondate")),
        deadline=_parse_iso(rec.get("originalcorrectbydate")),
        confidence=1.0,  # structured feed, no extraction (Rule 1)
        status=RecordStatus.ACCEPTED,  # trusted source (Rule 10)
        citations=[
            citations.socrata_row(
                DATASET_HPD,
                "violationid",
                violation_id,
                label=f"HPD violation #{violation_id} (NYC Open Data)",
                retrieved_at=now,
            ),
            citations.hpd_online(retrieved_at=now),
        ],
        extras={
            "violation_class": vclass,
            "nov_description": rec.get("novdescription"),
            "current_status": rec.get("currentstatus"),
            "apartment": rec.get("apartment"),
            "zip": rec.get("zip"),
            "rent_impairing": rec.get("rentimpairing"),
            "nov_issued_date": rec.get("novissueddate"),
        },
        extracted_at=now,
    )


def _dob_permit_to_event(rec: Mapping[str, Any]) -> CivicEvent:
    now = datetime.now(UTC)
    job_type = rec.get("job_type")
    addr = _address(rec.get("house__"), rec.get("street_name"))
    boro_digit = _BORO_NAME_TO_DIGIT.get((rec.get("borough") or "").upper())
    label = _JOB_TYPE_LABEL.get(job_type or "", job_type or "permit")
    # permit_si_no is unique per issued permit; fall back to a composite job key.
    permit_si_no = rec.get("permit_si_no")
    record_id = str(
        permit_si_no
        or "-".join(str(rec.get(k, "")) for k in ("job__", "job_doc___", "permit_sequence__"))
    )
    # Only a real permit_si_no yields a resolvable Socrata row link; a composite
    # fallback id is NOT a filterable column, so we omit the data_source link rather
    # than emit one that 404s (the BIS building link still lets a reader verify).
    permit_citations: list[Citation] = []
    if permit_si_no:
        permit_citations.append(
            citations.socrata_row(
                DATASET_DOB,
                "permit_si_no",
                str(permit_si_no),
                label=f"DOB permit {permit_si_no} (NYC Open Data)",
                retrieved_at=now,
            )
        )
    # Verify against the building's permit list, keyed by BIN, so the link lands on the
    # exact building the permit is for — not a development lot that reads as "no permits"
    # by address. Fall back to the block/lot property profile when the row carries no BIN.
    building_link = citations.dob_permits_by_bin(
        rec.get("bin__"), retrieved_at=now
    ) or citations.bis_property(boro_digit, rec.get("block"), rec.get("lot"), retrieved_at=now)
    if building_link:
        permit_citations.append(building_link)
    return CivicEvent(
        source_id=SOURCE_ID_DOB,
        source_record_id=record_id,
        # Prefer the row's own canonical BBL over re-deriving from block/lot (condo lots).
        bbl=_record_bbl(rec.get("bbl")) or bbl(boro_digit, rec.get("block"), rec.get("lot")),
        action_type="permit",
        title=f"DOB {job_type} permit ({label})" if job_type else "DOB permit",
        summary=(
            f"DOB issued a {label} permit at {addr or 'this building'}"
            + (
                f" for {rec['owner_s_business_name'].title()}"
                if rec.get("owner_s_business_name")
                else ""
            )
            + "."
        ),
        address=addr,
        event_date=_parse_mdy(rec.get("issuance_date")),
        latitude=_to_float(rec.get("gis_latitude")),
        longitude=_to_float(rec.get("gis_longitude")),
        confidence=1.0,
        status=RecordStatus.ACCEPTED,
        citations=permit_citations,
        extras={
            "job_type": job_type,
            "permit_type": rec.get("permit_type"),
            "permit_status": rec.get("permit_status"),
            "work_type": rec.get("work_type"),
            "bin": rec.get("bin__"),
            "nta_name": rec.get("gis_nta_name"),
            "owner_business_name": rec.get("owner_s_business_name"),
            "permittee_business_name": rec.get("permittee_s_business_name"),
            "issuance_date": rec.get("issuance_date"),
            "dob_run_date": rec.get("dobrundate"),
        },
        extracted_at=now,
    )


# --------------------------------------------------------------------------- #
# Declarative feed registry (dlt-style)                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SocrataFeed:
    """Declarative description of one Socrata dataset pull (dlt-style).

    Attributes:
        source_id: Stable connector id used in the SoR key (Rule 15).
        dataset_id: Socrata 4x4 dataset id on NYC Open Data.
        primary_key: Field(s) uniquely identifying a record -> ``source_record_id``.
        mapper: Record -> :class:`CivicEvent` transform for this feed.
        scope_where: SoQL predicate restricting the pull to the prototype boundary
            (East Harlem). NYC-specific; kept here, not in the shared core (Rule 4).
        incremental_cursor: Field to page incrementally on (default ``:updated_at``).
        domain: Socrata host.
    """

    source_id: str
    dataset_id: str
    primary_key: tuple[str, ...]
    mapper: Callable[[Mapping[str, Any]], CivicEvent]
    scope_where: str
    incremental_cursor: str = ":updated_at"
    domain: str = SOCRATA_DOMAIN


HPD_VIOLATIONS_FEED = SocrataFeed(
    source_id=SOURCE_ID_HPD,
    dataset_id=DATASET_HPD,
    primary_key=("violationid",),
    mapper=_hpd_violation_to_event,
    scope_where=f"zip in {_sql_in(EAST_HARLEM_ZIPS)}",
)
DOB_PERMITS_FEED = SocrataFeed(
    source_id=SOURCE_ID_DOB,
    dataset_id=DATASET_DOB,
    primary_key=("permit_si_no",),
    mapper=_dob_permit_to_event,
    scope_where=f"community_board = '{EAST_HARLEM_CB}'",
)


# --------------------------------------------------------------------------- #
# Public surface                                                              #
# --------------------------------------------------------------------------- #
def iter_feed(
    feed: SocrataFeed,
    since: str | None = None,
    where: str | None = None,
    limit: int | None = None,
    order: str = ":id",
) -> Iterator[CivicEvent]:
    """Pull one Socrata feed (scoped to the prototype boundary) and yield events.

    Args:
        feed: The declarative feed config to pull.
        since: Optional cursor value; yield only records at/after it on
            ``feed.incremental_cursor``. ``None`` does a full (scoped) backfill.
        where: Optional extra SoQL predicate AND-ed onto the scope (e.g. a date or
            class filter for the displacement signal).
        limit: Optional cap on records yielded (handy for demos/tests).

    Yields:
        One :class:`CivicEvent` per Socrata record, keyed by
        ``(feed.source_id, primary_key)`` (Rule 15). No LLM (Rule 1).
    """
    settings = get_settings()
    if settings.socrata_app_token is None:
        log.warning(
            "SOCRATA_APP_TOKEN not set — Socrata requests are unauthenticated and may be"
            " rate-limited. Get a free token at data.cityofnewyork.us."
        )
    client = Socrata(feed.domain, settings.socrata_app_token, timeout=_TIMEOUT)

    clauses = [f"({feed.scope_where})"]
    if where:
        clauses.append(f"({where})")
    if since:
        clauses.append(f"{feed.incremental_cursor} >= '{since}'")
    where_clause = " AND ".join(clauses)

    fetched = 0
    offset = 0
    try:
        while True:
            page = _get_page(
                client,
                feed.dataset_id,
                where=where_clause,
                limit=_PAGE,
                offset=offset,
                order=order,  # default ":id" for stable offset pagination
            )
            if not page:
                break
            for rec in page:
                yield feed.mapper(rec)
                fetched += 1
                if limit is not None and fetched >= limit:
                    return
            offset += _PAGE
    finally:
        client.close()


def discover_displacement_signals(asof: date | None = None) -> Iterator[CivicEvent]:
    """Emit buildings flagged by the displacement signal (cross-feed BBL correlation).

    Signal (NYC-SPECIFIC, tunable): a Class C HPD violation in the last
    ``DISPLACEMENT_VIOLATION_WINDOW_DAYS`` days AND an A1/NB/DM DOB permit in the
    last ``DISPLACEMENT_PERMIT_WINDOW_DAYS`` days on the SAME BBL. Correlates the
    two feeds on BBL (Rule 15) and threads the building on ``bbl:<BBL>`` (Rule 7).

    Args:
        asof: Reference date for the lookback windows (default: today).

    Yields:
        One REVIEW-status :class:`CivicEvent` per flagged building, with the
        contributing violation/permit ids and a sample quote in ``extras``.

    Note:
        Status is REVIEW, never ACCEPTED: do NOT ship until a tenant organizer
        validates ~20 flagged buildings as plausible (Rule 9; SOURCES.md gate).
    """
    asof = asof or date.today()
    violation_since = (asof - timedelta(days=DISPLACEMENT_VIOLATION_WINDOW_DAYS)).isoformat()
    permit_since = (asof - timedelta(days=DISPLACEMENT_PERMIT_WINDOW_DAYS)).isoformat()

    violations_by_bbl: dict[str, list[CivicEvent]] = defaultdict(list)
    for ev in iter_feed(
        HPD_VIOLATIONS_FEED,
        where=f"class = '{DISPLACEMENT_VIOLATION_CLASS}' AND inspectiondate >= '{violation_since}'",
    ):
        if ev.bbl:
            violations_by_bbl[ev.bbl].append(ev)

    permits_by_bbl: dict[str, list[CivicEvent]] = defaultdict(list)
    # Bound network volume server-side by dobrundate (ISO), then keep only permits whose
    # actual issuance_date is inside the window — dobrundate marks reprocessing, not issue.
    permit_cutoff = asof - timedelta(days=DISPLACEMENT_PERMIT_WINDOW_DAYS)
    job_filter = "job_type in " + _sql_in(DISPLACEMENT_PERMIT_JOB_TYPES)
    for ev in iter_feed(
        DOB_PERMITS_FEED,
        where=f"{job_filter} AND dobrundate >= '{permit_since}'",
    ):
        if ev.bbl and ev.event_date and ev.event_date >= permit_cutoff:
            permits_by_bbl[ev.bbl].append(ev)

    flagged = sorted(set(violations_by_bbl) & set(permits_by_bbl))
    log.info(
        "displacement signal: %d violation-BBLs x %d permit-BBLs -> %d flagged buildings",
        len(violations_by_bbl),
        len(permits_by_bbl),
        len(flagged),
    )
    for b in flagged:
        yield _displacement_event(b, violations_by_bbl[b], permits_by_bbl[b], asof)


def _displacement_event(
    building_bbl: str,
    violations: list[CivicEvent],
    permits: list[CivicEvent],
    asof: date,
) -> CivicEvent:
    now = datetime.now(UTC)
    addr = next((v.address for v in violations if v.address), None) or next(
        (p.address for p in permits if p.address), None
    )
    coords = next(((p.latitude, p.longitude) for p in permits if p.latitude), (None, None))

    # --- Quantify: the numbers that make the claim checkable (deterministic, Rule 1) ---
    v_dates = sorted(v.event_date for v in violations if v.event_date)
    p_dates = sorted(p.event_date for p in permits if p.event_date)
    deadlines = sorted(v.deadline for v in violations if v.deadline)
    latest_violation = v_dates[-1] if v_dates else None
    latest_permit = p_dates[-1] if p_dates else None
    earliest_due = deadlines[0] if deadlines else None
    days_overdue = (asof - earliest_due).days if earliest_due and earliest_due < asof else None
    job_types = sorted({str(jt) for p in permits if (jt := p.extras.get("job_type"))})
    job_labels = ", ".join(_JOB_TYPE_LABEL.get(jt, jt) for jt in job_types) or "major work"
    owner = next(
        (p.extras["owner_business_name"] for p in permits if p.extras.get("owner_business_name")),
        None,
    )

    summary = (
        f"{len(violations)} immediately-hazardous (Class C) HPD violation(s)"
        + (f", most recent {latest_violation.isoformat()}," if latest_violation else "")
        + f" AND {len(permits)} major-work permit(s) ({job_labels})"
        + (f", most recent issued {latest_permit.isoformat()}," if latest_permit else "")
        + f" on the same building (BBL {building_bbl}) within the signal windows "
        + f"({DISPLACEMENT_VIOLATION_WINDOW_DAYS}d violations / "
        + f"{DISPLACEMENT_PERMIT_WINDOW_DAYS}d permits)."
    )
    if earliest_due is not None and days_overdue is not None:
        summary += (
            f" Earliest violation correct-by deadline ({earliest_due.isoformat()}) "
            f"is {days_overdue} days overdue."
        )
    if owner:
        summary += f" Permit owner of record: {owner.title()}."
    summary += " This co-occurrence can precede tenant displacement; verify before acting."

    # --- Verify: aggregate every contributing record's link so a reviewer can check each ---
    boro_d, block, lot = building_bbl[0], str(int(building_bbl[1:6])), str(int(building_bbl[6:10]))
    signal_citations: list[Citation] = []
    bis = citations.bis_property(boro_d, block, lot, retrieved_at=now)
    if bis:
        signal_citations.append(bis)
    signal_citations.append(citations.hpd_online(retrieved_at=now))
    # The exact row backing each contributing violation/permit (machine-verifiable).
    for ev in (*violations, *permits):
        signal_citations.extend(c for c in ev.citations if c.kind == "data_source")

    return CivicEvent(
        source_id=SOURCE_ID_DISPLACEMENT,
        source_record_id=building_bbl,
        bbl=building_bbl,
        project_thread_id=f"bbl:{building_bbl}",  # Rule 7
        action_type="displacement_signal",
        title=f"Possible tenant-displacement risk at {addr or f'BBL {building_bbl}'}",
        summary=summary,
        address=addr,
        latitude=coords[0],
        longitude=coords[1],
        confidence=0.5,  # a correlation, not a verified fact
        status=RecordStatus.REVIEW,  # human-validate before shipping (Rule 9)
        citations=signal_citations,
        extras={
            "violation_count": len(violations),
            "permit_count": len(permits),
            "violation_ids": [v.source_record_id for v in violations],
            "permit_ids": [p.source_record_id for p in permits],
            # Quantified, checkable fields (each traces to a cited row above).
            "most_recent_violation_date": (
                latest_violation.isoformat() if latest_violation else None
            ),
            "most_recent_permit_date": latest_permit.isoformat() if latest_permit else None,
            "earliest_correct_by_date": earliest_due.isoformat() if earliest_due else None,
            "days_overdue": days_overdue,
            "permit_job_types": job_types,
            "owner_business_name": owner,
            "sample_violation": violations[0].extras.get("nov_description"),
            "sample_permit_job_type": permits[0].extras.get("job_type"),
        },
        extracted_at=now,
    )


# --------------------------------------------------------------------------- #
# Runnable demo: `python -m ingest.sources.nyc.dob_hpd`                         #
# --------------------------------------------------------------------------- #
def _demo() -> None:
    # ASCII-only output so it prints on any console (Windows cp1252 included).
    def show(ev: CivicEvent) -> str:
        when = ev.event_date.isoformat() if ev.event_date else "n/a"
        return f"  [{when}] {ev.title}  |  {ev.address or 'n/a'}  |  BBL {ev.bbl or 'n/a'}"

    print("\n=== East Harlem HPD violations (5 most recent) ===")
    for ev in iter_feed(HPD_VIOLATIONS_FEED, limit=5, order="inspectiondate DESC"):
        print(show(ev))

    print("\n=== East Harlem DOB A1/NB/DM permits (5 recent, issued 2025-2026) ===")
    recent_issue = (
        "job_type in ('A1','NB','DM') "
        "AND (issuance_date like '%2025' or issuance_date like '%2026')"
    )
    for ev in iter_feed(DOB_PERMITS_FEED, where=recent_issue, limit=5, order="dobrundate DESC"):
        print(show(ev))

    print("\n=== DISPLACEMENT SIGNAL - flagged buildings (first 8) ===")
    for i, ev in enumerate(discover_displacement_signals()):
        if i >= 8:
            print("  ... (more) ...")
            break
        print(f"  * {ev.address or ev.bbl}  (BBL {ev.bbl})  status={ev.status.value}")
        print(f"      {ev.summary}")
        print(f"      verify ({len(ev.citations)} link(s)):")
        for c in ev.citations:
            print(f"        - [{c.kind}] {c.label}: {c.url}")


if __name__ == "__main__":
    _demo()
