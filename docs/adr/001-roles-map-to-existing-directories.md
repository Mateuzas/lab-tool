# ADR 001: Roles map onto the existing directories

**Status:** accepted. **Date:** 2026-11-13.

**Context.** The Architect Protocol assumes `backend/ frontend/ infra/`. kali-lab already has `app/`,
`container/`, `host/`, `tests/` and a working CI that paths depend on.

**Decision.** Keep the layout. AGENTS.md maps each role to directories (Backender: `bounty/`,
`container/bin`, `host/`, `app/src-tauri/`, `tests/`; Frontender: `app/src/`; DevOps: Dockerfile,
CI, Makefile, SOPS). New Bug bounty code goes in a new top-level `bounty/` package.

**Consequences.** + No churn, open branches keep working. − Ownership is per directory list rather than
one folder per role, so briefs must name paths explicitly.
