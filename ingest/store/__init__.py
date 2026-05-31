"""Store stage package (CITY-AGNOSTIC).

Stage 5 of the assembly line: a verified record -> Postgres+PostGIS, with source
quotes attached per field. ONE canonical ``events`` table for every city;
per-source extras live in a JSONB column (adding a city needs no migration).

This package is city-agnostic — it never mentions NYC. NYC-specific identity
(BBL, ULURP, GeoSupport) is produced upstream in Normalize and arrives as plain
fields/JSONB.

Public surface (see db.py): upsert_event, write_quarantine, get_or_create_thread.
Schema: schema.sql.
"""
