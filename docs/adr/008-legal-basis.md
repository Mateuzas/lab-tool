# ADR 008: Every program profile has a legal basis

**Status:** accepted. **Date:** 2026-11-13.

**Context.** Platform programs grant permission through their rules and safe harbour. Lithuania's Law
on Cyber Security (Article 25 per NKSC) allows searching without permission under conditions: no
disruption, no copying data beyond proof, no password guessing, no social engineering, stop at the
first finding, notify within 24 h of starting and every 24 h while it continues. Lithuanian programs
are parked, but the model should not need changing when they come back.

**Decision.** `program.json` has `legal_basis`: `program` (rules of engagement apply) or
`lt-article-25` (the NKSC route). The latter is passive-only for Claude, blocks brute-force tools in
the guard and shows a 24 h notification timer. v1 implements only `program`.

**Consequences.** + The legal footing is explicit per program and enforceable. − The article text must
be checked against the official consolidated law before `lt-article-25` is implemented.
