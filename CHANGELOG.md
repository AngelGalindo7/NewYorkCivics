# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0 (`0.x`) means
the public surface — schema, connectors, digest format — may still change between releases.

No versions are tagged yet; everything below is **[Unreleased]**.

## [Unreleased]

### Added
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
