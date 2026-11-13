# kali-lab

Mac app with three modes: **Lab** (HTB/THM labs for beginners, working), **Bug bounty** (real
programs, optimised for an experienced hunter, being built) and **Analyze** (scam-message analysis,
parked). How it's built: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

The main session is the **Architect** and follows the Architect Protocol. Every other session is one of
the three roles (Backender, Frontender, DevOps) and follows its section in AGENTS.md: own only your
directories, and report contract problems instead of editing `docs/`.

This file is for building the tool. The Claude that *uses* the tool runs from `tutor/` (Lab) or
`hunter/` (Bug bounty) with its own CLAUDE.md and permissions.

@AGENTS.md
@ARCHITECT.md
