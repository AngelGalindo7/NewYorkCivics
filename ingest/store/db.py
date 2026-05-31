"""DB writer — persist verified records to Postgres+PostGIS (CITY-AGNOSTIC).

Stage: Store (Stage 5). Single responsibility: idempotent writes to the canonical
``events`` table, the ``quarantine`` table, and the ``project_threads`` table.
The DDL these functions target lives in ``schema.sql`` (same directory).

Rules honored here:
  - Rule 15 (SoR key): writes key on ``(source_id, source_record_id)``; BBL is the
            cross-source join key, carried as a column.
  - Rule 7  (project_thread_id from day one): get_or_create_thread resolves it.
  - Rule 2  (Fail fast, don't guess): non-validating records go to quarantine,
            never guessed into ``events``.
  - Rule 3  (Quote the source): per-field provenance is persisted as JSONB.
  - Rule 10 (Confidence routing): confidence + status columns are written.
  - Rule 6  (config flag): connection comes from DATABASE_URL, never hard-coded.

CITY-AGNOSTIC: no NYC specifics. Geocoded/normalized NYC values (BBL, CD) arrive
as plain fields/JSONB from upstream Normalize.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # psycopg is a runtime dep (requirements.txt). Typed here only; the stub does
    # not import it at module load so the package imports before deps install.
    from psycopg import Connection


# TODO Phase 1: read DATABASE_URL via ingest.config.get_settings() (Rule 6), not from
# os.environ directly, so tests can inject a connection string.
DATABASE_URL_ENV = "DATABASE_URL"

# Path to the DDL this module targets (see schema.sql in this directory).
SCHEMA_SQL_PATH = "ingest/store/schema.sql"


def upsert_event(
    conn: Connection,
    *,
    source_id: str,
    source_record_id: str,
    event: dict[str, Any],
    provenance: dict[str, Any],
    bbl: str | None = None,
    project_thread_id: str | None = None,
    confidence: float | None = None,
    status: str = "review",
    extras: dict[str, Any] | None = None,
) -> str:
    """Idempotently write one verified event; return its row id.

    Contract: UPSERT on the system-of-record key ``(source_id, source_record_id)``
    (Rule 15). ``provenance`` is the per-field source-quote map (Rule 3).
    ``confidence``/``status`` drive routing (Rule 10). Per-source novel fields go
    in ``extras`` JSONB — no per-source columns (one canonical table).
    """
    raise NotImplementedError(
        "Phase 1: UPSERT into events ON CONFLICT (source_id, source_record_id); "
        "see schema.sql. Persist provenance/confidence/status/extras as JSONB."
    )


def write_quarantine(
    conn: Connection,
    *,
    source_id: str,
    source_record_id: str | None,
    reason: str,
    raw: dict[str, Any],
) -> str:
    """Park a failed/flagged record in the quarantine table; return its row id.

    Contract: Rule 2 (Fail fast, don't guess) — anything that fails validation is
    written here with a human-readable ``reason`` and the ``raw`` payload, NEVER
    guessed into ``events``. Worked via the triage-quarantine skill.
    """
    raise NotImplementedError(
        "Phase 1: INSERT into quarantine (source_id, source_record_id, reason, raw)."
    )


def get_or_create_thread(
    conn: Connection,
    *,
    bbl: str | None = None,
    ulurp_number: str | None = None,
) -> str:
    """Resolve or allocate a project_thread_id linking records about one project.

    Contract: Rule 7 (project_thread_id from day one). The same project recurs
    across sources (CB agenda -> ZAP filing -> CEQR milestone -> Council vote); a
    thread id links them into one story. Match on BBL and/or ULURP number; create
    a new thread when no match exists.
    """
    raise NotImplementedError(
        "Phase 1: SELECT existing thread by bbl/ulurp_number, else INSERT a new "
        "project_threads row; return its id."
    )
