# ADR 007: Programs are ranked by a transparent heuristic, tuned on our own results

**Status:** accepted. **Date:** 2026-11-13.

**Context.** The user wants synced programs ranked by likely payout, low competition and likelihood of
vulnerabilities. None of these is observable directly; the platform exposes proxies at best.

**Decision.** `programs rank` computes four factor scores (payout, findability, competition,
friction) from whatever the API exposes, with weights in `config/ranking.json` (CONTRACTS §4). Every
factor and input is shown; missing inputs are skipped, never guessed. Hard filters exclude programs we
can't or shouldn't test. `programs changes` makes new programs and new scope visible immediately.
Later, hours and outcomes per program (`time.jsonl`, findings) are compared with the rank so the
weights are tuned on evidence, not opinion.

**Consequences.** + Explainable, adjustable, gets better with use. − v1 is educated guessing; competition
in particular is only approximated (age, confidentiality, counts if exposed). No ML until there is
enough of our own data to justify it.
