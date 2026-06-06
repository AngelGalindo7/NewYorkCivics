# TODO — NYC Civic Data Ingestion

## Done

### Commit 1 — Initial scaffolding (`5a79bc6`)
- Full directory layout: `ingest/{sources,parse,extract,normalize,store,deliver,eval,tests}/`
- `ingest/config.py` — config loader with `EXTRACT_MODEL` / `JUDGE_MODEL` flags (Rule 6)
- `ingest/observability.py` — log/trace seam
- All stage modules stubbed as `NotImplementedError` contracts
- `ingest/extract/schemas.py` — Pydantic `CivicEvent` + related schemas (city-agnostic)
- `ingest/extract/ulurp_codes.py` — NYC ULURP action-type lookup
- `ingest/extract/prompts/cb_agenda.v1.md` — first extraction prompt draft
- `ingest/parse/` — pdfplumber text path + vision-LLM fallback + router
- `ingest/normalize/` — GeoSupport geocode stub + zoning validator
- `ingest/store/schema.sql` — Postgres+PostGIS schema with `project_thread_id`, BBL, JSONB, quarantine table
- `ingest/store/db.py` — DB writer stub
- `ingest/deliver/` — subscribers, PostGIS match, linear-combo ranker, digest, send stubs
- `ingest/eval/` — promptfoo.yaml stub + Inspect AI `tasks.py` + sample golden label
- `ingest/tests/test_smoke.py` — import-safety + schema contract smoke tests
- CI (`ci.yml`: lint+type+test) + evals (`evals.yml`: promptfoo PR comment)
- `pyproject.toml` (ruff/mypy/pytest), `requirements.txt`, `requirements-dev.txt`, `Makefile`, `.env.example`

### Commit 2 — HPD + DOB East Harlem connector (`e2eca59`)
- `ingest/sources/nyc/dob_hpd.py` — full working connector (replaces stub):
  - Pulls HPD violations (`wvxf-dwi5`, ZIP-scoped) via Socrata
  - Pulls DOB permits (`ipu4-2q9a`, `community_board=111`) via Socrata
  - Maps both feeds → city-agnostic `CivicEvent` shape (no LLM, Rule 1)
  - Deterministic plain-English summaries per record
  - Cross-feed displacement signal: Class C violation (90d) + Alt-1/NB/DM permit (180d) on same BBL → `status=REVIEW`
  - Filters permits by `issuance_date` (not `dobrundate`) to avoid reprocessing inflation
  - Retry/backoff on flaky Socrata pages (tenacity)
- `ingest/config.py` — added `get_settings()` to surface `SOCRATA_APP_TOKEN`
- Verified live against East Harlem data (2026-05-31); displacement signal returns plausible buildings

### Commit 3 — End-to-end verifiable East Harlem digest (`cbf8603`)
- `ingest/sources/nyc/citations.py` — citation builder (source-quote links per record, Rule 3)
- `ingest/sources/nyc/harlem_digest.py` — East Harlem end-to-end runner (fetch → rank → digest)
- `ingest/deliver/digest.py` — digest generator expanded with real formatting logic
- `ingest/deliver/match.py` — PostGIS proximity match (250m / 500m / ZIP) implemented
- `ingest/deliver/rank.py` — linear-combo ranker implemented (Rule 8)
- `ingest/deliver/send.py` — send stub expanded; human-review gate wired (Rule 9)
- `ingest/extract/schemas.py` — additional fields for digest rendering
- `ingest/tests/test_deliver_digest.py` — digest unit tests (126 lines)

### Commit 4 — ZAP/ULURP East Harlem connector (`7c30a11`)
- `ingest/sources/nyc/zap_api.py` — full ZAP connector (replaces stub):
  - Pulls ZAP projects (`hgx4-8ukb`) + applicant contacts (`2iga-a6mk`) from Socrata
  - ULURP number validation + action-type lookup via `ulurp_codes.py`
  - Maps ZAP records → `CivicEvent` (no LLM, Rule 1)
  - Deadline scoring for ranker
- `ingest/sources/nyc/citations.py` — ZAP citation support added
- `ingest/sources/nyc/harlem_digest.py` — ZAP feed wired into the digest runner
- `ingest/tests/test_zap_ulurp.py` — ZAP connector tests (241 lines)

### Commit 5 — Phase 1 structured connectors complete
- `ingest/sources/nyc/legistar.py` — full Legistar connector (replaces stub):
  - Calls NYC Legistar REST API (`webapi.legistar.com/v1/nyc`) via httpx + tenacity
  - `discover_events(since)` — streams upcoming Council hearings / LU Committee items
  - `discover_cd_hearings(cd, days_ahead=30)` — Phase 1 gate query (upcoming 30 days)
  - `fetch_roll_call(matter_id)` — per-member votes via EventItems + Votes endpoints
  - Maps to `CivicEvent` (no LLM, Rule 1); `action_type` = `land_use_hearing` or `council_hearing`
  - `project_thread_id = "legistar:matter:{id}"` ready for ZAP threading in Phase 2
- `ingest/normalize/geocode.py` — GeoSupport wired (replaces stub):
  - Wraps `geosupport` Python package (ctypes shim over GeoSupport binaries)
  - Module-level singleton; returns `GeoResult(ok=False)` gracefully when binaries absent
  - `_detect_borough` from ZIP / address text; `_split_address` for house_number + street_name
  - Extracts BBL, BIN, community_district, lat/lng via Function 1B
- `ingest/eval/geocode_eval.py` — geocoding eval script:
  - Reads `ingest/eval/fixtures/geocode_addresses.csv`; runs `geocode()` on each address
  - Reports ok_rate, bbl_match_rate, median_error_m, p95_error_m vs Phase 1 targets (<50m/<500m)
  - Exits non-zero on gate failure; use `python -m ingest.eval.geocode_eval`
- `ingest/eval/fixtures/geocode_addresses.csv` — 20 East Harlem fixture addresses (1 BBL confirmed)
- `ingest/sources/nyc/harlem_digest.py` — Legistar wired into `gather_live_events`
  - New `include_legistar` / `legistar_days` params; Phase 1 gate comment inline
- `ingest/deliver/send.py` + `ingest/config.py` + `.env.example` — `BYPASS_HUMAN_REVIEW` flag:
  - Dev-only override for Rule 9 human-review-then-send gate (see rationale in .env.example)
  - Logs a loud warning; never silently bypasses; documented as production-prohibited
- `ingest/tests/test_legistar.py` — 29 offline contract tests (source identity, routing, citations)

---

## Up Next

### Phase 0 gate (partially met — finish before Phase 2)
- [ ] Hand-label **10** CB agenda PDFs → golden set (`ingest/eval/golden/`) via `/add-golden-doc`
- [ ] Verify promptfoo eval CI actually posts a PR comment (evals.yml wired but not smoke-tested end-to-end)
- [ ] Set per-field accuracy targets in `ingest/eval/promptfoo.yaml` (ULURP ≥70%, zoning ≥70%)

### Phase 1 remaining tasks
- [x] `ingest/sources/nyc/legistar.py` — implemented (Commit 5)
- [ ] `ingest/sources/nyc/cb_agenda.py` — Phase 2 (see cb_agenda stub; NOT Phase 1)
- [x] GeoSupport wiring — `normalize/geocode.py` wired; **still needs binaries + GEOSUPPORT_GEOFILES env to actually run**
- [ ] Geocoding eval: expand fixture to **100** addresses + populate `ref_lat`/`ref_lon` once GeoSupport binaries are installed; run `python -m ingest.eval.geocode_eval`
- [ ] Displacement signal organizer review — validate ~20 flagged buildings before shipping (Rule 9 / pivot threshold); use `BYPASS_HUMAN_REVIEW=true` for dev runs before review is complete
- [x] Phase 1 gate: `discover_cd_hearings("MN11", days_ahead=30)` wired in harlem_digest; run `python -m ingest.sources.nyc.harlem_digest` to verify live dates

### Phase 2
- [ ] `cb_agenda.py` full fetch/parse/extract loop — cluster ~59 boards by website template (expect 5–10 fetchers)
- [ ] Expand golden set to **50** docs clustered by layout
- [ ] Source-grounding eval (Rule 3) + parse fallback routing (text → vision per page)
- [ ] Signup form + subscriber table seeded
- [ ] Human-review queue UI / CLI for clearing digest candidates (Rule 9)
- [ ] Pre-committed user receives a digest and confirms it's useful (hard gate)

### Phase 3
- [ ] Snapshot regression in CI on golden 50
- [ ] Confidence routing (Rule 10): high → auto-accept, ~0.4–0.6 → review queue, <~0.4 → unverified/dropped
- [ ] Drift detection: validation-failure ratio per scraper, alert past ~15%; nightly re-fetch 5 URLs/source
- [ ] Per-source daily token budgets + circuit-breaker to deterministic fallback
