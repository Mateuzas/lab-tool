# ADR 009: Intigriti first, through its Researcher API

**Status:** accepted. **Date:** 2026-11-13.

**Context.** The user picked Intigriti (EU-based, many top programs, has an account). Program pages
render only with JavaScript; the Researcher API (personal access token) exposes programs, domains and
rules of engagement. Endpoint paths differ between Intigriti's release notes (`/v1/programs…`) and
community tools (`/core/researcher/programs…`).

**Decision.** `bounty/intigriti.py` is the only importer for now. BE-1 explores the live API with the
user's token first and records the real fields; CONTRACTS §2 and §4 are then finalised. Raw responses
are kept so the normaliser can change without re-fetching. Respect API rate limits; sync at most
every few hours.

**Consequences.** + Scope and rules arrive structured. − We depend on a beta API that may change; the raw
copies and a parse-error exit code (4) make breakage visible.
