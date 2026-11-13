# ADR 004: Claude in Bug bounty is active with approval, gated by program rules

**Status:** accepted. **Date:** 2026-11-13.

**Context.** In Lab mode Claude may not act on targets (HTB §6.1, THM §9.2). Bug bounty has no
blanket ban; rules are per program. Intigriti's Community Code of Conduct allows AI if its use is
disclosed and every finding is verified by a human with a working proof of concept. Many programs
forbid or limit automation.

**Decision.** The `hunter/` Claude Code project may read everything in the program folder and Burp
history (read-only MCP), and may run commands only through `bounty-run --actor claude`, which goes
through the scope guard and needs the user's approval per command. If the profile says
`automation: forbidden` or `ai_use: forbidden`, active tools are refused for Claude and it falls back
to reading, suggesting and drafting. Every report records AI use, generated from the flow log. Claude
never submits reports.

**Consequences.** + Claude can do real work where allowed, every request is attributable. − Approval per
command is slower; revisit (e.g. approval per plan) once the guard has a track record.
