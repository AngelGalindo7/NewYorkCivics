"""Normalize & validate stage — verify facts against official reference data.

Stage: Normalize & validate (stage 4 of the assembly line).
Single responsibility: take a clean ``CivicEvent`` and verify/enrich its
identifiers against official NYC reference data — address -> BBL/BIN/Community
District via GeoSupport, zoning codes vs the canonical list, ULURP format —
failing fast into quarantine on anything that doesn't validate.

Boundary: CITY-AGNOSTIC orchestration that *calls* NYC-specific services. The
orchestration (validate each field, route to quarantine) knows nothing about
NYC; the NYC details (GeoSupport, canonical zoning list) sit behind labeled
modules.

Dominant rules: Rule 2 (fail fast, don't guess -> quarantine),
Rule 13 (per-field accuracy targets — validate fields independently).
"""
