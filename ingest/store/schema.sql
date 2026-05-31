-- =============================================================================
-- NYC Civic ingestion — first-draft Postgres + PostGIS DDL (CITY-AGNOSTIC).
--
-- This is a STARTING POINT for Phase 1, not a final schema. Evolving parts are
-- marked `-- TODO Phase N`. Authoritative narrative lives in docs/DATA_MODEL.md;
-- this file owns the DDL.
--
-- Core decision: ONE canonical `events` table for every city; per-source extras
-- in a JSONB `extras` column (Councilmatic / Open Civic Data pattern) so adding a
-- city or source needs NO migration (Rule 16 - no premature abstraction).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS postgis;  -- enables geography(Point,4326) + <-> KNN

-- -----------------------------------------------------------------------------
-- project_threads — one row per real-world project. Rule 7 (project_thread_id +
-- JSONB extras from day one): the same project recurs across sources
-- (CB agenda -> ZAP -> CEQR -> Council vote); a thread links them into one story.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS project_threads (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bbl             VARCHAR(10),          -- Borough-Block-Lot; cross-source join key (Rule 15)
    ulurp_number    TEXT,                 -- e.g. 'C 240123 ZMM' (one project may span several)
    title           TEXT,                 -- human label, e.g. "215 West 96th St rezoning"
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    -- TODO Phase 2: thread resolution is heuristic (BBL/ULURP match). Revisit
    -- when dedup / entity-resolution eval (pairwise F1) is in place.
);
CREATE INDEX IF NOT EXISTS idx_threads_bbl   ON project_threads (bbl);
CREATE INDEX IF NOT EXISTS idx_threads_ulurp ON project_threads (ulurp_number);

-- -----------------------------------------------------------------------------
-- events — the one canonical table. Verified records only (Rule 2: failures go to
-- quarantine, never here). Per-source novel fields live in `extras` JSONB.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- System-of-record key (Rule 15): per-source identity is unique; BBL joins
    -- across sources.
    source_id           TEXT NOT NULL,        -- e.g. 'nyc_cb_agenda', 'nyc_hpd'
    source_record_id    TEXT NOT NULL,        -- the source's own id for this record
    bbl                 VARCHAR(10),          -- cross-source join key (nullable until geocoded)
    bin                 VARCHAR(7),           -- Building Identification Number, when known (derived in Normalize)

    -- Thread linkage (Rule 7).
    project_thread_id   UUID REFERENCES project_threads (id),

    -- Canonical fields shared by every city. City-specific identifiers (ULURP,
    -- CEQR, zoning) are first-class because they are the high-bar fields in the
    -- per-field accuracy targets (Rule 13), but they remain plain nullable text:
    -- a second city simply leaves them null.
    title               TEXT,
    action_type         TEXT,                 -- flat ~15-25 taxonomy (see docs/EVAL.md)
    address             TEXT,
    ulurp_number        TEXT,
    ceqr_number         TEXT,
    zoning_from         TEXT,
    zoning_to           TEXT,
    event_date          DATE,
    event_time          TIME,                 -- nullable; temporal eval is ±15 min
    deadline            DATE,                 -- action/comment deadline; drives Deliver "soonest deadlines first" + rank.py w_dl (Rule 8 / ADR-0005)
    summary             TEXT,                 -- plain-English summary, generated once in Extract

    -- Provenance (Rule 3 - Quote the source): per-field source-quote map
    -- { field: { value, source_quote, page, char_span } } so any field traces to
    -- its exact line in the original document.
    provenance          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Per-source extras — anything not in the canonical columns. No migration to
    -- add a source/city (Rule 16).
    extras              JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Confidence routing (Rule 10): high -> auto-accept, medium -> review queue,
    -- low -> unverified/dropped.
    confidence          REAL,                 -- 0.0-1.0
    status              TEXT NOT NULL DEFAULT 'review'
        -- NB: 'quarantined' is intentionally NOT allowed here — quarantined records route
        -- ONLY to the quarantine table, never into events (Rule 2). RecordStatus.QUARANTINED
        -- (schemas.py) is therefore invalid input to events.status.
        CHECK (status IN ('accepted', 'review', 'unverified')),

    -- Location for PostGIS <-> KNN radius queries (the three nested radii are
    -- resolved at match time in deliver/match.py: 250m / 500m+CD / ZIP+CD).
    geom                geography(Point, 4326),
    community_district  TEXT,                 -- CD for the 500m+CD and ZIP+CD match bands (derived in Normalize)
    zip                 VARCHAR(5),           -- derived in Normalize

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source_id, source_record_id)      -- Rule 15: SoR key
);
CREATE INDEX IF NOT EXISTS idx_events_bbl     ON events (bbl);
CREATE INDEX IF NOT EXISTS idx_events_thread  ON events (project_thread_id);
CREATE INDEX IF NOT EXISTS idx_events_date    ON events (event_date);
CREATE INDEX IF NOT EXISTS idx_events_status  ON events (status);
CREATE INDEX IF NOT EXISTS idx_events_cd      ON events (community_district);
CREATE INDEX IF NOT EXISTS idx_events_geom    ON events USING GIST (geom);  -- KNN <->
-- TODO Phase 1: add a GIN index on extras once query patterns on it are known.

-- -----------------------------------------------------------------------------
-- quarantine — Rule 2 (Fail fast, don't guess). Failed/flagged records land here
-- with a reason and the raw payload; they NEVER enter `events`. Worked via the
-- triage-quarantine skill; real failures get promoted into the golden set.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantine (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id           TEXT NOT NULL,
    source_record_id    TEXT,                 -- may be unknown if parse failed early
    reason              TEXT NOT NULL,        -- why it failed (validation/schema/geocode)
    raw                 JSONB NOT NULL,       -- the offending payload, verbatim
    confidence          REAL,                 -- 0.0-1.0 if known; drives the triage band (Rule 10)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_quarantine_source ON quarantine (source_id);

-- -----------------------------------------------------------------------------
-- subscribers — email signup is the ONLY v1 state (Rule 16: no accounts, no
-- passwords, no saved searches). GeoSupport is already wired in Normalize, so
-- geocoding a signup (address -> BBL/lat-lng/CD/ZIP) costs nothing extra.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscribers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               TEXT NOT NULL UNIQUE,
    address             TEXT NOT NULL,        -- as entered at signup
    bbl                 VARCHAR(10),          -- geocoded (Rule 15 join key)
    geom                geography(Point, 4326),  -- for the 250m/500m radius match
    community_district  TEXT,                 -- CD for the 500m+CD and ZIP+CD radii
    zip                 VARCHAR(5),
    confirmed_at        TIMESTAMPTZ,          -- double opt-in; NULL until confirmed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_subscribers_geom ON subscribers USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_subscribers_cd   ON subscribers (community_district);
-- TODO Phase 2: unsubscribe token / status column when delivery (send.py) ships.
