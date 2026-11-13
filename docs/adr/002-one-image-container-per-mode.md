# ADR 002: One image, one container per mode

**Status:** accepted. **Date:** 2026-11-13.

**Context.** Lab needs a VPN and an IP kill switch; Bug bounty needs direct internet behind a hostname
scope guard; Analyze will need no mounts and anonymised egress. Mixing them in one container would
mean one set of firewall rules trying to serve three threat models.

**Decision.** Build one image (`kali-lab:dev`, same tools). Each mode runs its own container from it
with its own supervisor, mounts and egress rules: `kali-lab` mounts `workspace/` (as today),
`kali-bounty` mounts only `workspace/bounty/`. Only one mode runs at a time (already in the app).

**Consequences.** + Each mode's rules are simple and testable alone; secrets and VPN files of one mode
are invisible to another. − The image carries tools both modes need; the image grows (acceptable).
