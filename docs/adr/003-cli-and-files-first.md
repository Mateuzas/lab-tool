# ADR 003: Bug bounty is CLI and plain files first, UI second

**Status:** accepted. **Date:** 2026-11-13.

**Context.** Bug bounty mode is for an experienced hunter and optimised for speed, not for beginners
(user decision). Lab mode was UI-first because the UI was the product. Here we don't yet know which
steps of a real hunt deserve screens.

**Decision.** Each slice ships as a `host/bounty` or in-container command writing plain files
(JSON/JSONL/Markdown) under `workspace/bounty/`. Screens come after the command has been used in a
real hunting session, read the same files, and are keyboard-first and dense. Claude uses the same
commands.

**Consequences.** + Fast to build, scriptable, testable, nothing is locked in the UI. − For a while the
mode is terminal-heavy; that's fine for its user.
