"""Deliver stage package (CITY-AGNOSTIC) — the payoff layer.

Stage 6 of the assembly line: match stored records to subscribers by location,
rank them, let a human clear a short review queue, then send a plain-English
email digest. This is a THIN query-and-send layer on top of already-stored
records — it adds no new extraction or LLM work (summaries were generated once in
Extract and cached).

The headline outcome: a neighbor reads one email and knows what they need to know
this week.

CITY-AGNOSTIC: no NYC specifics. Subscriber geocoding reuses the shared Normalize
geocoder; matching/ranking/digest know nothing about NYC.

Rules that dominate: Rule 9 (human-review-then-send), Rule 10 (confidence
routing / mark unverified), Rule 8 (linear-combo ranker), Rule 16 (no accounts —
email signup is the only v1 state).

Public surface: subscribers, match, rank, digest, send.
"""
