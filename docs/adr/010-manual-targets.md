# ADR 010: Manual targets (direct authorisation)

**Status:** accepted. **Date:** 2026-11-15.

**Context.** The tool was built around Intigriti programs, but the same scope guard, evidence log and
Burp wiring are exactly what you want when testing a system whose owner has asked you directly (a
friend's pre-launch site, a client with a signed scope). The guard already enforces any profile with
a valid scope and rate limit; it does not read `legal_basis`.

**Decision.** Add `legal_basis: "authorization"` and a `manual` profile source
(`host/bounty manual add`, CONTRACTS §2.1) that writes a `platform: "manual"` program.json with the
authorisation recorded (owner, a path to the written permission, dates). Manual targets are enforced
like any program but are excluded from the Intigriti sync, ranking, change feed and notifications
(`platform != "intigriti"`). Only create one when the owner has authorised the testing in writing;
the profile stores where that authorisation is kept.

**Consequences.** + One tool for platform and direct-authorisation work, same guard and evidence.
− The handle namespace now has `manual-*`; the app's `u` key stays Intigriti-only, so manual targets
are driven from the CLI until an app path is added. Legal responsibility for authorisation rests with
the user; the tool records it but cannot verify it.
