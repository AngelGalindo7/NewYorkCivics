# Security & Responsible Disclosure

This project ingests public NYC government data and delivers it to residents. Two classes of
report matter here, and **both** are treated as security-sensitive:

1. **Software vulnerabilities** — anything that could compromise the pipeline, leak secrets/keys,
   or expose subscriber data (emails, addresses).
2. **Resident-harm / data-integrity issues** — a civic claim that is wrong or misleading in a way
   that could harm a resident (a false displacement flag, a hallucinated hearing date), or any
   output that exposes a person who should not have been named.

A *confidently wrong* civic fact is, for this project, a safety issue — not just a bug.

## Reporting

**Do not open a public issue for a vulnerability or a resident-harm report.** Disclosure before a
fix can put residents at risk.

- Report privately to **`<INSERT PRIVATE CONTACT — e.g. a security email, or GitHub "Report a
  vulnerability" under the Security tab>`**.
- Include: what you found, how to reproduce it, and (for data-integrity reports) the specific
  record and its source link so we can verify against the city's own system.
- We aim to acknowledge within **a few days** and to agree on a disclosure timeline with you.

Routine, non-sensitive data-quality problems (a stale link, a formatting glitch) can instead go to
the **data-quality** issue template — use private reporting only when disclosure itself could cause
harm.

## Supported versions

Pre-1.0: only the `main` branch is supported. There are no long-term support branches yet.

## Scope notes

- **Secrets** live in `.env` (git-ignored); the committed reference is `.env.example`. If you ever
  find a key committed to history, report it privately — do not paste it into an issue.
- **Subscriber PII** (email, address) is minimized by design. Reports of over-collection or
  over-retention are in scope.
- **Third-party eval/trace tools** (e.g. Langfuse Hobby) have their own retention terms; reports
  that civic PII may be leaving the box uninspected are in scope.

Thank you for disclosing responsibly.
