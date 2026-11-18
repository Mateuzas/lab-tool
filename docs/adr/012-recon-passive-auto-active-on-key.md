# ADR 012: Recon: passive runs automatically, active only on a key press for the active program

**Status:** accepted. **Date:** 2026-11-15.

**Context.** Finding *where to look* (new hosts, live services, login and API surfaces) is where
automation pays most for one tester. But anything that touches a program's own hosts is target
traffic: it counts against the rate limit, may be forbidden by the rules, and must be attributable.
Passive sources (certificate transparency, DNS) send nothing to the program.

**Options.** (a) Everything manual: slow, new scope is missed. (b) Everything automatic, including
probing and crawling after each sync: traffic to programs nobody chose to test, possibly against
their rules. (c) Split by who receives the traffic.

**Decision.** (c). `recon passive` (crt.sh + the Mac's DNS, generic User-Agent) runs automatically:
after a sync for new in-scope wildcards on the active, followed or top-25 programs (≤ 3 per sync),
and on first `u` of a program. `recon map` (httpx-toolkit + katana) runs only when the user presses
`m` or types the command, only for the guard's **active** program, inside `kali-bounty` through the
guard as `tool:<name>`, at the program's rate, same host only, and not at all when the program
forbids automation. `recon list` ranks hosts by a simple, visible `interest` score (CONTRACTS §10).

**Consequences.** + New scope is noticed without any target traffic; every active request is a
deliberate choice, scope-checked and in the flow log. − Mapping has to be started by hand per
program; hosts of programs that aren't active are never probed. The interest score is keyword-based
and can miss or over-rate hosts; it orders the list, it doesn't judge.
