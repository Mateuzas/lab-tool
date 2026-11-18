# ADR 011: kali-bounty runs hardened: no spare capabilities, read-only root, state in tmpfs

**Status:** accepted. **Date:** 2026-11-15.

**Context.** `kali-bounty` runs tools, a shell and Claude's approved commands next to the scope
guard. The guard is only as strong as the container around it: a process that gains root
capabilities could rewrite the firewall, edit the policy or the evidence, or keep a change across
restarts. Docker's default capability set is larger than the supervisor needs, and a writable root
filesystem lets anything persist inside the container.

**Options.** (a) Docker defaults plus the firewall: too much ambient power. (b) A separate container
per process (guard, tools): more moving parts, harder file sharing with the work dir. (c) One
container, every capability dropped except the supervisor's own, no privilege escalation, read-only
root with tmpfs where run-time writes are needed.

**Decision.** (c). `--cap-drop=ALL` plus `NET_ADMIN`, `SETUID`, `SETGID`, `SETPCAP`, `CHOWN`,
`FOWNER`, `DAC_OVERRIDE`, `KILL` (firewall, switching to `guard`/`hunter`, preparing their
directories, killing a stray guard); guard and hunter run with none. `--security-opt
no-new-privileges:true`, `--read-only` with tmpfs for `/tmp`, `/run`, `/var/log`, `/etc/ssl/certs`,
`/usr/local/share/ca-certificates` (the trust store is rebuilt on every start). The policy snapshot
lives in `/dev/shm`. Hunter's home is the volume `kali-bounty-home`; guard state and flows stay in
`kali-bounty-guard`. The container carries `kali-lab.guard-spec=<n>`; `guard start` recreates an
older container and keeps both volumes (CONTRACTS §3.3).

**Consequences.** + Nothing inside the container can widen its own powers or persist outside the
two volumes and `/bounty/work`; a restart returns to a clean, fail-closed state (no profile until
`program use`). Upgrades happen on the next `guard start` without manual steps. − Tools that rely on
file capabilities (nmap) can't exec with an empty bounding set; they couldn't reach targets anyway
(HTTP(S) only, ADR 005). Changing the run flags means bumping the spec. After any container restart
the program must be loaded again.
