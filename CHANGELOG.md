# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0 (`0.x`) means
the public surface — schema, connectors, digest format — may still change between releases.

No versions are tagged yet; everything below is **[Unreleased]**.

## [Unreleased]

### Added
- **Phase 0 eval gate met** (`ingest/eval/promptfoo.yaml`): 10 per-field javascript assertions
  across all golden cases — ULURP format, `event_date` YYYY-MM-DD, `action_type` string,
  `ulurp_number === null` on non-ULURP cases, `zoning_from`/`zoning_to` on ZMA tests. Live run
  confirmed 10/10 PASS with the echo provider. ULURP regex tightened to `\s+` (spaces required).
- **10 hand-labeled golden CB agenda records** (`ingest/eval/golden/`): 5 boroughs, 8 action types,
  spanning ULURP/ZMA/special-permit/variance/CEQR/landmark/budget — each with a `_source_text`
  block and per-field `provenance.source_quote` entries (Rule 3).
- **Geocoding eval fixture expanded** (`ingest/eval/fixtures/geocode_addresses.csv`): 20 → 100 East
  Harlem addresses; 35 rows carry Nominatim `ref_lat`/`ref_lon` (validated in-bounds for CD11).
  Gate scores spatial accuracy once GeoSupport binaries are installed.
- **evals.yml CI hardened**: `npm ci` + `node-version: "20"` cache; `package-lock.json` pinned at
  `promptfoo ^0.121.0`; `package-lock.json` added to `paths:` trigger; fork-PR token downgrade
  documented inline (eval gate still enforces; only the PR diff comment is skipped on fork PRs).
- **`make check` now includes `fmt-check`**: closes the gap where `ruff format --check` passed in
  CI but was absent from the local `check` target.
- **ZAP/ULURP connector** (`ingest/sources/nyc/zap_api.py`): pulls NYC Zoning Application Portal
  land-use applications for East Harlem (Socrata `hgx4-8ukb`), joins BBLs from `2iga-a6mk`,
  maps to `CivicEvent` with citations, and threads related filings on
  `project_thread_id = "zap:{project_id}"` (Rule 7). Action type derived from ULURP action code
  via lookup table.
- **Legistar connector** (`ingest/sources/nyc/legistar.py`): calls the NYC Council Legistar REST
  API (`webapi.legistar.com/v1/nyc`) via httpx + tenacity; exposes `discover_events`,
  `discover_cd_hearings` (Phase 1 gate: upcoming hearings for the next 30 days), and
  `fetch_roll_call`.
- **GeoSupport geocoding** (`ingest/normalize/geocode.py`): wires the `geosupport` Python wrapper
  (Function 1B); returns `GeoResult(ok=False)` gracefully when binaries are absent; lazy-initialized
  so import is always clean.
- **Geocoding eval** (`ingest/eval/geocode_eval.py`): runs `geocode()` against a fixture CSV and
  reports `ok_rate`, `bbl_match_rate`, `median_error_m`, `p95_error_m`; exits non-zero when Phase 1
  thresholds are missed (`median < 50 m`, `p95 < 500 m`).
- **`BYPASS_HUMAN_REVIEW` dev flag**: lets the full digest pipeline run end-to-end in local dev
  and CI without clearing the Rule 9 human-review queue; logs a loud warning; never silently
  bypasses in production.
- **Import-safety across all connectors**: `dob_hpd`, `zap_api`, `legistar`, and `config` all
  guard optional deps (`sodapy`, `httpx`, `tenacity`, `python-dotenv`) behind `try/except` so
  every module imports cleanly with only `pydantic` installed (CI requirement).
- **Contract tests** (`ingest/tests/test_zap_ulurp.py`, `test_legistar.py`): 467 lines of offline
  tests verifying source identity (Rule 15), project threading (Rule 7), confidence routing
  (Rule 10), citation audit (Rule 3), fail-fast (Rule 2), and the Rule 4 seam (ZAP events feed
  directly into the city-agnostic deliver pipeline).
- **Open-source governance:** `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, this
  `CHANGELOG.md`, a pull-request template, and issue forms (bug, feature, **data-quality**).

### Earlier work (pre-changelog, from git history)
- End-to-end **verifiable East Harlem digest** from live HPD/DOB feeds — group → rank →
  human-review → render, with per-claim citations.
- HPD + DOB East Harlem **structured connector** with the **displacement signal**.
- Initial **scaffolding**: the six-stage pipeline skeleton and the eval-first harness.

---

> When you add a release, move the relevant `[Unreleased]` items under a new
> `## [x.y.z] - YYYY-MM-DD` heading and start a fresh `[Unreleased]` section.
