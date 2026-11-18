# ADR 013: Two-account access-control check on demand; sessions only in tmpfs; unsafe methods opt-in

**Status:** accepted. **Date:** 2026-11-18.

**Context.** Broken access control between two test accounts is one of the most productive checks
on a deep program, and replaying requests as another user is mechanical. Tools like Autorize replay
every proxied request automatically. That conflicts with the hybrid strategy (no mass traffic), with
programs' rate limits, and with changing data: replaying a POST or DELETE as another user can modify
or destroy real state. The check needs live session cookies or tokens, which are secrets.

**Options.** (a) Automatic replay of all traffic (Autorize-like). (b) Replay in Burp by hand only.
(c) A command that replays only the requests the user picks, through the guard.

**Decision.** (c), CONTRACTS §13. `host/bounty ac session a|b` reads header lines from stdin (never
argv) into the container's tmpfs (`/dev/shm/bounty-ac/`, root 0600); `ac status` shows header names
only; a restart clears them. `ac candidates` lists account A's replayable 2xx requests from the
guard's flows. `ac replay` sends each chosen request as B and with no session, through the guard as
`tool:ac-check`, sequentially, after the helper drops to uid `hunter`. Only GET, HEAD and OPTIONS
are replayed unless the id is also given with `--unsafe`. Results store statuses, lengths,
similarity and verdicts, never bodies or header values. Every flag is a hint to confirm by hand.

**Consequences.** + No background replays, no session on disk, no data changed by accident, every
replay in the flow log. − Sessions must be set again after each container restart; the user picks
requests by hand; similarity verdicts can be wrong both ways. App screens for it (FE-9) are still to
come; until then it is CLI only.
