"""Extract stage — parsed content + strict schema -> JSON facts.

Stage: Extract (stage 3 of the assembly line).
Single responsibility: turn a uniform ``ParsedDoc`` into structured facts that
conform to the canonical clean-record schema, with a verbatim source quote on
every field.

Boundary: CITY-AGNOSTIC machinery. The schema-driven extractor knows nothing
about NYC. The only NYC-specific file in this package is ``ulurp_codes.py``,
which is loudly labeled NYC-SPECIFIC.

Dominant rules: Rule 1 (LLM only on dirty inputs), Rule 3 (quote the source),
Rule 6 (model name behind a config flag).
"""
