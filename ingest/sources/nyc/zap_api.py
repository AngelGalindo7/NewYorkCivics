"""Stage 1 (Fetch) — NYC ZAP land-use applications via Socrata. NYC-SPECIFIC, STRUCTURED.

Single responsibility: pull NYC Zoning Application Portal (ZAP) land-use application
records from NYC Open Data (Socrata dataset ``hgx4-8ukb``), join with per-project BBL
data (dataset ``2iga-a6mk``), map them to the canonical
:class:`~ingest.extract.schemas.CivicEvent` shape, and thread related filings onto one
``project_thread_id``. Both datasets are clean structured JSON, so this connector emits
records directly and SKIPS Parse and Extract entirely (Rule 1).

Resident value: "what land-use changes are being proposed in my neighborhood" — formal
ULURP applications that alter zoning, permit large developments, or trigger environmental
review. These are the formal-rezoning complement to the HPD/DOB building-level signals.

SNAPSHOT vs INCREMENTAL
-----------------------
ZAP is exposed as a snapshot, not an incremental stream — there is no reliable
``:updated_at`` cursor on the ZAP dataset. Each run re-pulls the current scoped set;
the store layer deduplicates on ``(source_id, source_record_id)`` (Rule 15). Compare
``dob_hpd``, which *does* support ``:updated_at`` cursors for incremental backfill.

THREADING (Rule 7)
------------------
One ZAP project generates multiple ULURP applications (e.g. a rezoning + a special
permit + an environmental review). We thread all filings on
``project_thread_id = "zap:{project_id}"`` — the *project* is the story, not each
individual application number.

BBL JOIN
--------
BBLs live in a separate dataset (``2iga-a6mk``, one row per project-BBL pair). This
connector pulls the primary dataset first, collects ``project_id``s, then batch-fetches
BBLs in chunks and enriches each event. Events without a BBL match are still emitted
with ``bbl=None`` (fail-soft; the store layer handles null BBLs via address matching).

Rules honored
-------------
- Rule 1  (LLM only on dirty inputs): structured -> NO LLM, ever. Plain-English
          summaries here are deterministic templates, not generation.
- Rule 4  (NYC-specific code in nyc/): dataset ids, CD11 scope, ZAP URL format stay here.
- Rule 7  (project_thread_id + JSONB): ``project_thread_id = "zap:{project_id}"``; per-
          source quirks (all ULURP numbers, lead action, applicant) go in ``extras``.
- Rule 10 (confidence routing): structured -> ``confidence=1.0``, ``status=ACCEPTED``.
- Rule 15 (SoR key = (source_id, source_record_id) + BBL): per-record identity;
          ``source_record_id = project_id``.

================================ DECISION RECORD ================================
ZAP connector scope (2026-06-01) — step 2 of ADR 0007.

  WHAT: pull ZAP land-use applications for East Harlem via the Socrata datasets
        documented in ADR 0007 (hgx4-8ukb primary + 2iga-a6mk BBL join).
  WHERE: community_district = 'MN11' (Manhattan Community District 11, East Harlem).
  DATASETS (verified live in ADR 0007, 2026-05-31):
    - hgx4-8ukb: ZAP projects; confirmed real fields: ulurp_numbers, project_brief,
                 public_status. Field aliases handle ZAP schema evolution (log on use).
    - 2iga-a6mk: project-BBL rows; 26,991 Manhattan rows per ADR 0007.
  THREADING: project_thread_id = "zap:{project_id}" links related applications for the
    same development. The store layer can then join ZAP events with HPD/DOB events on
    BBL to build one cross-source story per building (Rule 7).
  SNAPSHOT: no :updated_at cursor on hgx4-8ukb; full scoped re-pull + store dedup per run.
=================================================================================
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Any

from requests.exceptions import RequestException
from sodapy import Socrata
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ingest.config import get_settings
from ingest.extract.schemas import CivicEvent, RecordStatus
from ingest.observability import get_logger
from ingest.sources.nyc import citations

log = get_logger(__name__)

SOURCE_ID = "nyc_zap"

# Socrata dataset ids (verifiable row link built from these — see citations.py).
DATASET_ZAP = "hgx4-8ukb"  # ZAP projects: ulurp_numbers, project_brief, public_status
DATASET_ZAP_BBL = "2iga-a6mk"  # project-BBL rows (many-to-many; project_id -> bbl)

SOCRATA_DOMAIN = "data.cityofnewyork.us"
_PAGE = 1000  # Socrata default cap per request; we paginate by offset.
_TIMEOUT = 60  # seconds
_BBL_BATCH = 200  # max project_ids per IN clause (keeps SoQL URL length safe)

# East Harlem scope — Manhattan Community District 11 (NYC-SPECIFIC, Rule 4).
EAST_HARLEM_CD = "MN11"


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
) -> list[dict[str, Any]]:
    """One Socrata page fetch with retry/backoff on transient HTTP errors (Rule 2)."""
    return client.get(dataset_id, where=where, limit=limit, offset=offset, order=":id")


# --------------------------------------------------------------------------- #
# Small deterministic helpers                                                  #
# --------------------------------------------------------------------------- #
def _sql_in(values: tuple[str, ...]) -> str:
    """Render a SoQL IN list from trusted Socrata identifiers (alphanumeric)."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "")).date()
    except ValueError:
        return None


def _address(*parts: str | None) -> str | None:
    joined = " ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


def _first_ulurp(raw: str | None) -> str | None:
    """First ULURP number from a comma- or pipe-separated string."""
    if not raw:
        return None
    first = raw.replace("|", ",").split(",")[0].strip()
    return first or None


# --------------------------------------------------------------------------- #
# Record -> CivicEvent mapper                                                  #
# --------------------------------------------------------------------------- #
def _zap_project_to_event(rec: Mapping[str, Any], bbl_value: str | None = None) -> CivicEvent:
    """Map one ZAP project row (hgx4-8ukb) to a CivicEvent.

    Args:
        rec: Raw Socrata row from dataset ``hgx4-8ukb``.
        bbl_value: Optional BBL joined from ``2iga-a6mk`` (first BBL if multiple).

    Raises:
        ValueError: if the record has no usable ``project_id`` — cannot build the
            SoR key (Rule 15 / Rule 2 fail-fast).
    """
    now = datetime.now(UTC)

    # project_id is the stable SoR key (Rule 15); aliases handle schema evolution.
    project_id = str(rec.get("project_id") or rec.get("id") or "").strip()
    if not project_id:
        raise ValueError("ZAP record missing project_id — cannot construct SoR key (Rule 15)")

    # Confirmed real fields per ADR 0007; aliases for dataset evolution.
    ulurp_raw = str(rec.get("ulurp_numbers") or rec.get("ulurp_number") or "").strip()
    project_brief = str(rec.get("project_brief") or rec.get("description") or "").strip()
    public_status = str(rec.get("public_status") or "").strip()
    applicant = str(rec.get("applicant_name") or rec.get("applicant") or "").strip()
    lead_action = str(rec.get("lead_action") or "").strip()
    community_district = str(rec.get("community_district") or "").strip()
    borough = str(rec.get("borough") or "").strip()
    address = _address(rec.get("primary_address") or rec.get("address"))

    # Dates: ZAP uses ISO 8601 strings.
    event_date = _parse_iso(rec.get("certified_referred") or rec.get("certified_date"))
    # Hearing date is the most actionable deadline for a resident.
    hearing_raw = (
        rec.get("hearing_date") or rec.get("hearing_date_1") or rec.get("public_hearing_date")
    )
    deadline = _parse_iso(str(hearing_raw) if hearing_raw is not None else None)

    ulurp_first = _first_ulurp(ulurp_raw)

    # Title: lead action + first ULURP + current public status.
    title_parts: list[str] = [lead_action or "Land-use application"]
    if ulurp_first:
        title_parts.append(f"({ulurp_first})")
    if public_status:
        title_parts.append(f"— {public_status}")
    title = " ".join(title_parts)

    # Summary: project_brief is the resident-facing content; key facts appended.
    summary_parts: list[str] = [project_brief or f"ZAP project {project_id}."]
    if applicant:
        summary_parts.append(f"Applicant: {applicant}.")
    if public_status:
        summary_parts.append(f"Status: {public_status}.")
    if ulurp_raw:
        summary_parts.append(f"ULURP: {ulurp_raw}.")
    summary = " ".join(summary_parts)

    return CivicEvent(
        source_id=SOURCE_ID,
        source_record_id=project_id,
        bbl=bbl_value,
        project_thread_id=f"zap:{project_id}",  # Rule 7: one thread per project
        action_type="rezoning",
        title=title,
        summary=summary,
        address=address,
        ulurp_number=ulurp_first,  # first (or only) ULURP in the canonical field
        event_date=event_date,
        deadline=deadline,
        confidence=1.0,  # structured feed, no extraction (Rule 1)
        status=RecordStatus.ACCEPTED,  # trusted source (Rule 10)
        citations=[
            citations.socrata_row(
                DATASET_ZAP,
                "project_id",
                project_id,
                label=f"ZAP project {project_id} (NYC Open Data)",
                retrieved_at=now,
            ),
            citations.zap_project(project_id, retrieved_at=now),
        ],
        extras={
            "project_id": project_id,
            "ulurp_numbers": ulurp_raw or None,
            "public_status": public_status or None,
            "applicant_name": applicant or None,
            "lead_action": lead_action or None,
            "community_district": community_district or None,
            "borough": borough or None,
            "certified_referred": rec.get("certified_referred"),
            "hearing_date": hearing_raw,
        },
        extracted_at=now,
    )


# --------------------------------------------------------------------------- #
# BBL enrichment — dataset 2iga-a6mk (project_id -> bbl, many-to-many)        #
# --------------------------------------------------------------------------- #
def _fetch_bbls_for_projects(
    client: Socrata,
    project_ids: tuple[str, ...],
) -> dict[str, list[str]]:
    """Batch-fetch ``project_id -> [bbl, ...]`` from the ZAP BBL dataset.

    Chunks ``project_ids`` into batches of ``_BBL_BATCH`` to stay within Socrata
    SoQL URL-length limits. Returns an empty dict if ``project_ids`` is empty.
    """
    if not project_ids:
        return {}
    result: dict[str, list[str]] = defaultdict(list)
    for i in range(0, len(project_ids), _BBL_BATCH):
        chunk = project_ids[i : i + _BBL_BATCH]
        where = f"project_id in {_sql_in(chunk)}"
        offset = 0
        while True:
            page = _get_page(client, DATASET_ZAP_BBL, where=where, limit=_PAGE, offset=offset)
            if not page:
                break
            for row in page:
                pid = str(row.get("project_id") or "").strip()
                bbl_val = str(row.get("bbl") or "").strip()
                if pid and bbl_val:
                    result[pid].append(bbl_val)
            offset += _PAGE
    return dict(result)


# --------------------------------------------------------------------------- #
# Public surface                                                               #
# --------------------------------------------------------------------------- #
def iter_zap_events(
    since: str | None = None,
    limit: int | None = None,
) -> Iterator[CivicEvent]:
    """Pull ZAP land-use applications for East Harlem and yield CivicEvents.

    Two-phase pull (snapshot — see module docstring):
      1. Page through ``hgx4-8ukb`` filtered to ``community_district='MN11'``
         (East Harlem). Collect all project rows in memory.
      2. Batch-fetch BBLs from ``2iga-a6mk`` for the collected ``project_id``s.
      3. Yield one :class:`CivicEvent` per project, ``bbl`` set where available.

    Args:
        since: Accepted for interface symmetry with other connectors; has NO effect
            (ZAP is snapshot-only — no ``:updated_at`` cursor on ``hgx4-8ukb``).
        limit: Optional cap on events yielded (handy for demos/tests).

    Yields:
        One :class:`CivicEvent` per ZAP project, ``status=ACCEPTED`` (Rule 10).
        Records missing a ``project_id`` are skipped with a warning (Rule 2).
    """
    if since:
        log.debug("zap: since=%s ignored — snapshot-only feed, no incremental cursor", since)

    settings = get_settings()
    client = Socrata(SOCRATA_DOMAIN, settings.socrata_app_token, timeout=_TIMEOUT)
    scope = f"community_district = '{EAST_HARLEM_CD}'"

    project_rows: list[dict[str, Any]] = []
    offset = 0
    try:
        # --- Phase 1: collect all CD11 projects ---
        while True:
            page = _get_page(client, DATASET_ZAP, where=scope, limit=_PAGE, offset=offset)
            if not page:
                break
            project_rows.extend(page)
            offset += _PAGE
        log.info("zap: pulled %d project rows for %s", len(project_rows), EAST_HARLEM_CD)

        # --- Phase 2: batch-fetch BBLs ---
        project_ids = tuple(
            str(r.get("project_id") or r.get("id") or "").strip() for r in project_rows
        )
        project_ids = tuple(pid for pid in project_ids if pid)
        bbl_map = _fetch_bbls_for_projects(client, project_ids)
        log.info("zap: %d of %d projects have >=1 BBL", len(bbl_map), len(project_ids))

    finally:
        client.close()

    # --- Phase 3: emit events ---
    emitted = 0
    for rec in project_rows:
        project_id = str(rec.get("project_id") or rec.get("id") or "").strip()
        if not project_id:
            log.warning("zap: skipping row with no project_id: %s", dict(rec))
            continue
        bbls = bbl_map.get(project_id, [])
        bbl_value = bbls[0] if bbls else None
        try:
            yield _zap_project_to_event(rec, bbl_value)
        except ValueError as exc:
            log.warning("zap: skipping project %s: %s", project_id, exc)
            continue
        emitted += 1
        if limit is not None and emitted >= limit:
            return


# --------------------------------------------------------------------------- #
# Runnable demo: `python -m ingest.sources.nyc.zap_api`                       #
# --------------------------------------------------------------------------- #
def _demo() -> None:
    def show(ev: CivicEvent) -> str:
        when = ev.event_date.isoformat() if ev.event_date else "n/a"
        return (
            f"  [{when}] {ev.title}"
            f"  |  {ev.address or 'n/a'}"
            f"  |  BBL {ev.bbl or 'n/a'}"
            f"  |  thread={ev.project_thread_id}"
        )

    print("\n=== East Harlem ZAP land-use applications (10 most recent) ===")
    for ev in iter_zap_events(limit=10):
        print(show(ev))
        print(f"      ULURP: {ev.ulurp_number}  status: {ev.extras.get('public_status')}")
        brief = (ev.summary or "")[:120]
        print(f"      {brief}...")
        print(f"      Verify ({len(ev.citations)} link(s)):")
        for c in ev.citations:
            print(f"        [{c.kind}] {c.label}: {c.url}")


if __name__ == "__main__":
    _demo()
