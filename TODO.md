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

### Phase 0 gate (MET — all checks complete)
- [x] Hand-label **10** CB agenda PDFs → golden set (`ingest/eval/golden/`) via `/add-golden-doc`
- [x] Set per-field accuracy targets in `ingest/eval/promptfoo.yaml` — javascript assertions on all 10 tests (ULURP non-null, event_date YYYY-MM-DD, action_type string, ulurp_number=null on non-ULURP, zoning_from/to on ZMA cases); eval gate confirmed 10/10 PASS; ULURP regex tightened to `\s+`
- [x] evals.yml CI audited — fork-PR comment limitation documented; `package-lock.json` committed to pin promptfoo `^0.121.0`; `npm ci` step added to workflow; `package-lock.json` added to paths trigger
- [x] `make check` now includes `fmt-check` (`ruff format --check`) — gap between local and CI closed
- [x] `tasks.py` eval solver source_id de-hardcoded — reads from golden record metadata (Rule 4 seam clean)
- [x] **Smoke-test**: `workflow_dispatch` trigger added to `evals.yml` — manually trigger from GitHub Actions UI to verify PR comment posting without a full PR push. Local run confirmed 10/10 PASS. `make eval` target already existed.

### Phase 1 remaining tasks
- [x] `ingest/sources/nyc/legistar.py` — implemented (Commit 5)
- [ ] `ingest/sources/nyc/cb_agenda.py` — Phase 2 (see cb_agenda stub; NOT Phase 1)
- [x] GeoSupport wiring — `normalize/geocode.py` wired with **NYC GeoSearch HTTP fallback** (`_geosearch_fallback` via stdlib urllib); eval now runs without binaries: ok_rate 100%, median 39.5 m (target <50 m), PASS. Install GeoSupport binaries + `GEOSUPPORT_GEOFILES` env to unlock p95 gate and exact BBL matching.
- [x] Geocoding eval: runs end-to-end (`python -m ingest.eval.geocode_eval`); gate PASS in GeoSearch-fallback mode; p95 gate informational until GeoSupport binaries installed.
- [x] `BYPASS_HUMAN_REVIEW=true` set in `.env` for dev runs — displacement signal organizer review still needed before production send.
- [x] Phase 1 gate: `discover_cd_hearings("MN11", days_ahead=30)` wired in harlem_digest; run `python -m ingest.sources.nyc.harlem_digest` to verify live dates

---

### Immediate bugs (blocking live digest — fix before Phase 2a)

These are live failures visible in the digest runner today. Small fixes, high value.

- [x] **Legistar 403** — FIXED: the `webapi.legistar.com` read API is public/keyless (there is no self-serve token portal). The connector now requests keyless (token optional, only lifts rate limits) with a descriptive User-Agent, instead of short-circuiting to empty when `LEGISTAR_TOKEN` was unset. Unblocks Council/Land-Use hearing items in the digest.
- [x] **ZAP 0 results** — FIXED: the `hgx4-8ukb` dataset stores `community_district` as a borough-letter + 2-digit code (e.g. `M08`), so East Harlem is `M11`, not `MN11`. Confirmed live 2026-06-12 via the connector's 0-row self-diagnostic (it logged the real sample values). `EAST_HARLEM_CD` corrected to `M11`.
- [ ] **`SOCRATA_APP_TOKEN` unset** — every Socrata call logs a throttle warning. Add the token to `.env` (free at data.cityofnewyork.us). No behavior change, just removes the rate-limit risk.

---

### Phase 2a — MN11 proof-of-loop (one board, full loop, real subscriber)

**Goal:** Complete the fetch → parse → LLM-extract → digest → deliver loop for CD11 only.
Prove the pipeline is end-to-end useful before scaling to 59 boards. This is the hardest
architectural bet — if the PDF extractor produces garbage on real CB11 agendas, better to know
at one board than fifty. **Gate: one real East Harlem subscriber confirms the digest is useful.**

Each item below is one PR-sized unit of work.

- [ ] **`cb_agenda.py` MN11 fetcher** — implement `discover_agendas(board="MN11")` and `fetch(url)`:
  - Scrape the CD11 board website for agenda PDF links (httpx + BeautifulSoup or lxml; guard with `try/except ImportError`)
  - Return `AgendaRef` objects with URL, meeting date, title
  - `fetch()` downloads bytes; no parsing here — bytes go straight to the parse stage
  - Offline contract tests with a fixture HTML snapshot (no live calls in CI)
- [ ] **Wire into `harlem_digest.py`** — call `cb_agenda.discover_agendas` → `fetch` → `pdf_text.extract_text` → `extractor.extract` and merge resulting `CivicEvent` records into `gather_live_events`. Add `include_cb_agenda: bool` param (default off; flip on after spot-check passes).
- [ ] **Spot-check 3 real MN11 PDFs** — run the full loop on 3 recent CD11 agendas, print extracted events, manually verify field correctness against the source PDF. Document findings. This is the human gut-check before trusting the loop.
- [ ] **Add 3–5 MN11 golden docs** — promote the spot-checked PDFs to `ingest/eval/golden/` via `/add-golden-doc`. Expand golden set from 10 → ~15. These are the first docs extracted by a real LLM (not the echo provider).
- [ ] **Swap promptfoo from echo → real extractor for MN11 docs** — update `promptfoo.yaml` to use the Python file provider (`file://ingest/extract/extractor_cli.py`) for the new MN11 test cases. Keep the echo provider for the original 10 Phase 0 tests to preserve the zero-cost baseline. Gate: new MN11 tests pass at ≥70% field accuracy.
- [ ] **Human-review CLI** — `make review` (or `python -m ingest.deliver.review`) that lists pending digest candidates and lets the reviewer approve/reject each one interactively. Replaces `BYPASS_HUMAN_REVIEW=true` for local dev. The bypass flag stays in `.env.example` for CI; the CLI is for human operators.
- [ ] **Signup form + subscriber row** — a Google Form (or equivalent) that writes to a CSV / seeds the subscriber table with name + address + email. Needs at least one real East Harlem contact before the next gate.
- [ ] **Wire email delivery** — set `EMAIL_PROVIDER` (Resend or Postmark are simplest for v1); implement the Phase 2 `send()` adapter in `deliver/send.py`. Keep the file-sink fallback for local dev.
- [ ] **Hard gate: first real digest sent and confirmed useful** — one East Harlem resident or organizer receives a digest containing CB11 agenda items + HPD/DOB data and says it's actionable. No code gate — this is a human conversation. Nothing in Phase 2b ships until this is met.

---

### Phase 2b — Scale to all 59 NYC boards

**Gate:** Phase 2a hard gate met. Extraction accuracy ≥ 80% F1 on 50 golden docs.

- [ ] **Template audit** — manually categorize all ~59 NYC CB websites into clusters by site structure (Legistar-hosted, Outlook calendar export, raw PDF links, custom CMS, etc.). Document the clusters. Expect 5–10 distinct templates, not 59 fetchers.
- [ ] **One fetcher per template cluster** — implement `cb_agenda.py` fetchers for each cluster identified above, reusing the MN11 fetcher as the first reference implementation. Each fetcher is a thin function; the fetch → parse → extract pipeline is shared.
- [ ] **Expand golden set to 50 docs** — add real PDFs from each template cluster, distributed by layout type. Run `/add-golden-doc` for each. Target ≥ 5 docs per cluster.
- [ ] **Source-grounding eval** — eval harness scores per-field hallucination rate (target ≤ 1%). Every extracted fact must carry a `provenance.source_quote` traceable to the source PDF. Failures surface in the promptfoo diff comment.
- [ ] **Vision fallback routing** — implement `pdf_route.py` (char-count heuristic) and `pdf_vision.py` (vision-LLM per scanned page). Route only truly scanned pages to vision; text-extractable pages stay deterministic. Gate: no regression on the 50 golden docs.
- [ ] **Few-shot examples in extraction prompt** — add 3–5 diverse golden examples to `cb_agenda.v2.md` (never edit v1 in place). Eval diff must show ≥ 3pp F1 improvement before merging.

---

### Phase 3 — Production hardening

**Gate:** Phase 2b complete. Digest running for all 59 boards.

- [ ] **Snapshot regression in CI** — store expected extraction output for the golden 50; CI fails if any field regresses. Prevents silent prompt/model drift from breaking production silently.
- [ ] **Confidence routing** — route high-confidence (≥ 0.6) events to auto-accept, mid-band (0.4–0.6) to human-review queue, low (< 0.4) to unverified/dropped. Replace the linear threshold with a calibrated cutoff on the golden set.
- [ ] **Drift detection** — per-scraper validation-failure ratio tracked over time; alert when it crosses ~15%. Nightly re-fetch of 5 URLs per source. Prevents silent feed breakage from going unnoticed for weeks.
- [ ] **Per-source token budgets + circuit-breaker** — daily LLM token cap per source; if a source exceeds the budget or fails repeatedly, fall back to the structured/deterministic path and flag for human inspection. No runaway costs.
- [ ] **`BYPASS_HUMAN_REVIEW` removed from `.env`** — once the human-review CLI and confidence routing are in place, the bypass flag has no legitimate use case. Remove it from the codebase entirely; any future bypass is a code change requiring review.

---

### Phase 4 — Second city

**Gate:** Phase 3 complete. Digest is live, stable, and confirmed useful in East Harlem.
Do not start this phase until the MN11 loop has been running without incident for ≥ 2 weeks.

- [ ] **Pick the second city** — criteria: open civic data feeds (Socrata or equivalent), active community organizing context, willing early adopter as the first subscriber.
- [ ] **Port the pipeline** via `/port-new-city` — copy the NYC connector directory, swap the geocoder and lookup modules, reuse `parse`/`extract`/`eval`/`store`/`deliver` untouched. The seam test: if porting requires touching city-agnostic code, fix the seam first.
- [ ] **Validate seams** — city-agnostic machinery (`parse`, `extract`, `eval`, `store`, `deliver`) must have zero mentions of NYC in their diffs. Any leak is a blocker.
- [ ] **Golden set for city 2** — hand-label 10 documents from the new city's sources before shipping. Same eval harness, new fixture data.
