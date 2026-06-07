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

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # psycopg is a runtime dep (requirements.txt). Typed here only; the stub does
    # not import it at module load so the package imports before deps install.
    from psycopg import Connection

# Canonical columns that map directly from the event dict.
_EVENT_COLUMNS = frozenset(
    {
        "title",
        "action_type",
        "address",
        "ulurp_number",
        "ceqr_number",
        "zoning_from",
        "zoning_to",
        "event_date",
        "event_time",
        "deadline",
        "summary",
        "community_district",
        "zip",
    }
)

_VALID_STATUSES: frozenset[str] = frozenset({"accepted", "review", "unverified"})


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
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}")

    # Split event keys into canonical columns vs. overflow into extras.
    canonical: dict[str, Any] = {}
    overflow: dict[str, Any] = {}
    for key, value in event.items():
        if key in _EVENT_COLUMNS:
            canonical[key] = value
        elif key not in ("latitude", "longitude"):
            overflow[key] = value

    unexpected = canonical.keys() - _EVENT_COLUMNS
    if unexpected:
        raise ValueError(f"Event dict contains keys not in _EVENT_COLUMNS: {sorted(unexpected)}")

    # Merge explicit extras kwarg on top of overflow (explicit wins on collision).
    merged_extras: dict[str, Any] = {**overflow, **(extras or {})}

    latitude = event.get("latitude")
    longitude = event.get("longitude")
    has_geom = latitude is not None and longitude is not None

    # Build the INSERT column list and values dynamically so NULL columns are
    # still written explicitly — keeps ON CONFLICT DO UPDATE simple.
    insert_cols = [
        "source_id",
        "source_record_id",
        "bbl",
        "project_thread_id",
        "confidence",
        "status",
        "provenance",
        "extras",
        *canonical.keys(),
    ]
    insert_values: list[Any] = [
        source_id,
        source_record_id,
        bbl,
        project_thread_id,
        confidence,
        status,
        json.dumps(provenance),
        json.dumps(merged_extras),
        *canonical.values(),
    ]

    if has_geom:
        insert_cols.append("geom")

    placeholders = ", ".join(
        ["%s"] * len(insert_values)
        + (["ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography"] if has_geom else [])
    )
    if has_geom:
        insert_values.extend([longitude, latitude])

    # ON CONFLICT: update every non-key column plus updated_at.
    update_cols = [c for c in insert_cols if c not in ("source_id", "source_record_id")]
    update_clauses = ", ".join(
        "provenance = events.provenance || EXCLUDED.provenance"
        if c == "provenance"
        else f"{c} = EXCLUDED.{c}"
        for c in update_cols
    )
    update_clauses += ", updated_at = now()"

    sql = (
        f"INSERT INTO events ({', '.join(insert_cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (source_id, source_record_id) DO UPDATE SET {update_clauses} "
        f"RETURNING id"
    )

    with conn.cursor() as cur:
        cur.execute(sql, insert_values)
        row = cur.fetchone()

    return str(row[0])


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
    sql = (
        "INSERT INTO quarantine (source_id, source_record_id, reason, raw) "
        "VALUES (%s, %s, %s, %s) "
        "RETURNING id"
    )

    with conn.cursor() as cur:
        cur.execute(sql, [source_id, source_record_id, reason, json.dumps(raw)])
        row = cur.fetchone()

    return str(row[0])


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
    if bbl is None and ulurp_number is None:
        raise ValueError("At least one of bbl or ulurp_number must be provided")

    # Build a WHERE clause that OR-joins whichever keys are present.
    conditions: list[str] = []
    params: list[Any] = []
    if bbl is not None:
        conditions.append("bbl = %s")
        params.append(bbl)
    if ulurp_number is not None:
        conditions.append("ulurp_number = %s")
        params.append(ulurp_number)

    where = " OR ".join(conditions)
    select_sql = f"SELECT id FROM project_threads WHERE {where} LIMIT 1"

    # Phase 2: this SELECT→INSERT sequence has a TOCTOU race under concurrent workers.
    # Fix with serializable isolation or a UNIQUE constraint + INSERT ... ON CONFLICT.
    with conn.cursor() as cur:
        cur.execute(select_sql, params)
        row = cur.fetchone()
        if row is not None:
            return str(row[0])

        # No existing thread — insert one.
        insert_sql = "INSERT INTO project_threads (bbl, ulurp_number) VALUES (%s, %s) RETURNING id"
        cur.execute(insert_sql, [bbl, ulurp_number])
        row = cur.fetchone()

    return str(row[0])
