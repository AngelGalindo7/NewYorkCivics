"""Pydantic models for the CLEAN record shape (the contract Extract emits).

Stage: Extract (stage 3).
Single responsibility: define the canonical, city-agnostic shape of one
extracted civic event plus its per-field provenance, so every downstream stage
(Normalize, Store, Deliver) speaks the same record.

Boundary: CITY-AGNOSTIC. No NYC vocabulary leaks into these models — NYC
identifiers (BBL, ULURP) are plain strings here; their *meaning* and validation
live in ``ulurp_codes.py`` and ``ingest/normalize/``. Adding a city must not
require touching this file.

Rules honored:
- Rule 3 (quote the source): ``Provenance`` carries {value, source_quote, page,
  char_span} for every field — any field traces back to its exact line.
- Rule 7 (project_thread_id + JSONB extras from day one): ``project_thread_id``
  and ``extras`` are first-class fields.
- Rule 10 (confidence routing): ``confidence`` + ``status`` drive accept /
  review / unverified.
- Rule 13 (per-field accuracy targets): provenance is per-field so identifiers
  and fuzzy fields can be measured separately.
- Rule 15 (SoR key): ``(source_id, source_record_id)`` + ``bbl``.

These are minimal, first-draft model definitions (real fields + types), not
implementations. Shapes will firm up in Phase 0/1.

# TODO Phase 0: lock field names against the golden-set labeling schema
# (see ../eval/golden/README.md) before hand-labeling 10 CB agendas.
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RecordStatus(StrEnum):
    """Routing status for a record (Rule 10 — confidence routing).

    Drives the gate between Store and the human-review queue.
    """

    ACCEPTED = "accepted"  # high confidence -> auto-accept
    REVIEW = "review"  # uncertain band (~0.4-0.6) -> review queue
    UNVERIFIED = "unverified"  # below ~0.4 -> dropped or flagged in digest
    QUARANTINED = "quarantined"  # Rule 2 -> quarantine table ONLY; never a valid events.status


class Provenance(BaseModel):
    """Per-field source grounding (Rule 3 — quote the source).

    One ``Provenance`` per extracted field. ``source_quote`` is the verbatim
    sentence the value came from; the extracted value must appear in it (this is
    the hallucination check the eval harness runs).
    """

    value: Any = Field(description="The extracted value, as it appears post-parse.")
    source_quote: str = Field(description="Verbatim source sentence the value was taken from.")
    page: int | None = Field(default=None, description="1-based page in the source document.")
    char_span: tuple[int, int] | None = Field(
        default=None,
        description="(start, end) char offsets of the value within the parsed text.",
    )

    # TODO Phase 2: add a `match_kind` (exact | paraphrase) once the source-
    # grounding eval distinguishes verbatim hits from plausible paraphrases.


class Citation(BaseModel):
    """A verifiable link back to the authoritative source RECORD (sibling of Rule 3).

    ``Provenance`` quotes the verbatim *sentence* a fact came from — the right tool
    for dirty, extracted inputs. ``Citation`` links the authoritative *record* a fact
    came from — the right tool for clean structured feeds, which have no prose to quote.
    Together they make every asserted fact independently checkable by a reader: the
    digest can render "see the source" next to each claim.

    Two ``kind`` tiers carry different guarantees:
      - ``data_source``: the exact, machine-verifiable row backing the claim (e.g. the
        Socrata API row filtered by primary key). Pins the precise record used.
      - ``official_lookup``: a human-facing official page for the same subject (e.g. a
        city building-profile page) — friendlier to a resident, but not row-exact.

    ``verifies`` states HOW STRONGLY the link confirms the claim, so the digest never
    overclaims (e.g. a homepage search tool is not the same as the exact record):
      - ``exact_record``   : THIS row/permit/violation (strongest).
      - ``exact_building`` : the specific building's official page.
      - ``search``         : an official search tool, not pre-filled (weakest).

    At scale these URLs are pure functions of (source_id, source_record_id, bbl) — the
    Phase-1 target is to STORE identity and resolve URLs at render time via an injected
    city resolver, not to materialize URL strings onto every stored record (no dup, no
    stale links). They are materialized here only because there is no DB yet.
    """

    kind: str = Field(description="'data_source' (exact row) | 'official_lookup' (human page).")
    verifies: str = Field(
        default="exact_record",
        description="exact_record | exact_building | search — see class docstring.",
    )
    label: str = Field(description="Reader-facing link text, e.g. 'HPD violation #12345'.")
    url: str = Field(description="Resolvable URL to the authoritative record or lookup page.")
    retrieved_at: datetime | None = Field(
        default=None,
        description="When the linked record was fetched; structured feeds mutate, so pin the time.",
    )


class CivicEvent(BaseModel):
    """Canonical clean-record shape for one extracted civic event (CITY-AGNOSTIC).

    The single record every source normalizes into. The high-bar land-use
    identifiers (ULURP / CEQR / zoning) are first-class *nullable* fields
    (Rule 13); another city simply leaves them null. Per-source *quirks* (a DOB
    job number, an applicant name) go in ``extras`` (JSONB), never as new
    top-level fields (Rule 7).
    """

    # --- System-of-record identity (Rule 15) ---
    source_id: str = Field(
        description="Stable connector/source id, snake_case <city>_<source>, e.g. 'nyc_cb_agenda'."
    )
    source_record_id: str = Field(description="Source-native id; unique within source_id.")

    # --- Cross-source join keys ---
    bbl: str | None = Field(
        default=None,
        description="Borough-Block-Lot. Plain string here; validated in Normalize. (Rule 15)",
    )
    project_thread_id: str | None = Field(
        default=None,
        description="Links records about the same project across sources into one story. (Rule 7)",
    )

    # --- What happened ---
    action_type: str | None = Field(
        default=None,
        description="Flat action taxonomy value (e.g. rezoning, hearing, permit, violation).",
    )
    title: str | None = Field(default=None, description="Short human-facing label for the event.")
    summary: str | None = Field(
        default=None,
        description="Plain-English summary, generated once here and cached for Deliver.",
    )

    # --- Land-use identifiers (NYC today; plain nullable strings so another city
    # leaves them null — Rule 4 / Rule 13. Their meaning + validation live in
    # ulurp_codes.py and ingest/normalize/, never in this city-agnostic model). ---
    address: str | None = Field(
        default=None,
        description="Street address as stated in the source (raw; geocoded in Normalize).",
    )
    ulurp_number: str | None = Field(
        default=None,
        description="ULURP application number, e.g. 'C 240123 ZMM' (raw; validated in Normalize).",
    )
    ceqr_number: str | None = Field(
        default=None, description="CEQR (environmental review) number, if any."
    )
    zoning_from: str | None = Field(
        default=None,
        description="Existing zoning district, e.g. 'R7-2' (validated in Normalize).",
    )
    zoning_to: str | None = Field(default=None, description="Proposed zoning district, e.g. 'R8A'.")

    # --- Dates ---
    event_date: date | None = Field(default=None, description="Primary date of the event.")
    event_time: time | None = Field(
        default=None, description="Time of the event, if stated (temporal eval is ±15 min)."
    )
    deadline: date | None = Field(default=None, description="Action/comment deadline, if any.")

    # --- Geometry (lat/lng -> PostGIS Point(4326) in Store) ---
    latitude: float | None = Field(default=None, description="WGS84 latitude.")
    longitude: float | None = Field(default=None, description="WGS84 longitude.")

    # --- Confidence + routing (Rule 10) ---
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Extractor confidence; routes accept / review / unverified.",
    )
    status: RecordStatus = Field(
        default=RecordStatus.REVIEW,
        description="Routing status derived from confidence + validation.",
    )

    # --- Provenance (Rule 3) ---
    provenance: dict[str, Provenance] = Field(
        default_factory=dict,
        description="Map of field name -> Provenance. Every asserted fact should have an entry.",
    )

    # --- Citations (verifiable links back to the authoritative record) ---
    # NOT a stored column at scale (ADR 0008): the durable record keeps identity
    # (source_id, source_record_id, bbl) + reference ids, and URLs are resolved on demand
    # at digest render via an injected city resolver. Materialized here only pre-DB.
    citations: list[Citation] = Field(
        default_factory=list,
        description="Verifiable links to the source record(s); rendered next to a claim.",
    )

    # --- Per-source extras (Rule 7 — JSONB, no migration to add a city/source) ---
    extras: dict[str, Any] = Field(
        default_factory=dict,
        description="Source-specific fields that don't belong in the canonical shape.",
    )

    extracted_at: datetime | None = Field(
        default=None, description="When the extractor produced this record (audit trail)."
    )

    # TODO Phase 1: add geometry validation hook + BIN/community_district once
    # Normalize wires GeoSupport (these are derived, not extracted).
    # TODO Phase 2: tighten action_type to a Literal/Enum over the locked
    # ~15-25 type taxonomy (see docs/EVAL.md categorical row).
