# Architecture

kali-lab is a Mac desktop app (Tauri 2, "Lab Tool") that drives a Kali container in Docker Desktop.
It has three modes, and only one runs at a time:

| Mode | For | Status | Design goal |
|---|---|---|---|
| **Lab** | HTB / THM beginners | working | ease of use: plain language, no manual steps |
| **Bug bounty** | an experienced tester on Intigriti programs and manual targets | working, being extended | **speed for an experienced tester**: keyboard, density, automation where the program allows it |
| **Analyze** | scam messages (links, emails, attachments) | parked | isolation from hostile content |

Lab mode is documented in the README and `docs/spike-notes.md`. This document is mostly about Bug
bounty mode, plus the parts the modes share. Exact formats, exit codes and commands are in
[CONTRACTS.md](CONTRACTS.md); daily use is in [USER-GUIDE.md](USER-GUIDE.md).

## 1. Shared foundation

- **One image, one container per mode** (ADR 002). `kali-lab:dev` is built once; Lab runs it as
  `kali-lab` (VPN, kill switch), Bug bounty as `kali-bounty` (scope guard). Each has its own
  mounts, network rules and supervisor. Starting one stops the other (`host/bounty guard start`
  stops `kali-lab`; the app's `start_lab` stops the guard). Analyze will be a third profile.
- **The Mac controls containers with `docker exec`**, never by editing shared files (Docker Desktop
  serves stale copies). Results come back as files in the mode's workspace folder or as
  `docker exec` output.
- **CLI and files first** (ADR 003). The app runs the same CLIs the terminal uses (`host/lab`,
  `host/bounty`) and reads their JSON; it never talks to Intigriti or a target itself.
- **Secrets are SOPS/age encrypted** (ADR 006). Plaintext exists only in a process's environment
  (`sops exec-env`) or in a container's tmpfs, never on disk.
- **Containers are never `--privileged`**; ports publish on `127.0.0.1` only.

## 2. Bug bounty: components and data flow

```
 Mac                                                     kali-bounty container (read-only root, cap-drop ALL)
 ┌────────────────────────────────────────────┐          ┌──────────────────────────────────────────────────┐
 │ Lab Tool app (Tauri)                       │          │ bounty-supervisor (root, 8 caps)                 │
 │  Programs screen, look-here panel          │          │  ├ bounty-firewall: OUTPUT DROP except uid guard │
 │  timer: sync every ~20 min, notifications  │          │  ├ bounty-ctl: policy snapshot in /dev/shm      │
 │      │ spawns                              │          │  └ bounty-guard (uid guard): mitmdump + addon   │
 │      ▼                                     │          │      :8080 human / tools   :8082 claude          │
 │ host/bounty (Python, stdlib) ──HTTPS──► Intigriti API  │      scope · rate · identification · flow log    │
 │  programs sync/rank/changes                │          │                      │                           │
 │  recon passive ──HTTPS──► crt.sh + Mac DNS │          │                      ▼                           │
 │  program use / guard / flows / ac / map ───┼─docker──►│ bounty-run (uid hunter, proxy env, actor header) │
 │      │ writes                              │  exec    │  shell · httpx · katana · curl · ffuf · nuclei   │
 │      ▼                                     │          │ bounty-map (recon map) · bounty-ac (ac replay)   │
 │ workspace/bounty/ ─────────── bind mount ──┼─────────►│ /bounty (ro), /bounty/work (rw)                  │
 │                                            │          │ volume kali-bounty-guard: guard.json, flows, CA  │
 │ Burp (native) ──upstream 127.0.0.1:8081 ───┼─────────►│ volume kali-bounty-home: /home/hunter            │
 │ Terminal.app: host/bounty shell ───────────┼─docker──►│                                                  │
 │ Terminal.app: hunter/ Claude Code ─────────┼─docker exec … bounty-run --actor claude ─►               │
 └────────────────────────────────────────────┘          └───────────────────────┬──────────────────────────┘
                                                                                 ▼
                                                              in-scope targets (and recon sources) only
```

### 2.1 Program intake and ranking (Mac side, no target traffic)

- `host/bounty` talks only to the **Intigriti Researcher API** (ADR 009) with the token from
  `workspace/secrets/intigriti.enc.yaml`, read via `scripts/secrets.sh exec intigriti --`.
- `programs sync` is incremental: list + new activities, then detail only for programs that are new,
  changed, stale or never fetched (`--full` re-fetches all), ≤ 1 request/s. It writes a normalised
  `program.json` per program (CONTRACTS §2), and `raw/` + `snapshots/` only when something changed.
  A detail 403 is `needs-join` (re-checked after 24 h), not an error.
- `programs rank` scores programs with a transparent heuristic (ADR 007, CONTRACTS §4): payout ×
  findability × low competition, minus friction. `--explain <handle>` shows why.
- `programs changes` diffs syncs into `history.jsonl`: new programs, scope added/removed, bounty and
  rules changed, access granted/lost, program closed/reopened.
- `program apply-state` records where an application to a private program stands
  (`applications.json`); `access-granted` turns `applied` into `accepted`.
- **Manual targets** (ADR 010): `host/bounty manual add` writes `programs/manual-<slug>/program.json`
  with `legal_basis: authorization`. The guard enforces it like any program; sync, rank, changes and
  notifications ignore it.

### 2.2 Scope guard (container side, enforced; ADR 005)

The guard is the only thing in `kali-bounty` that can open connections to the internet:

- `bounty-firewall` sets iptables `OUTPUT` (IPv4 and IPv6) to **drop**; only uid `guard` may open
  new outbound connections, and DNS only for `guard`. A tool that ignores the proxy fails closed.
  The supervisor re-checks the rules every second; if they drift, it kills the guard and re-locks.
- `bounty-guard` is `mitmdump` with `guard_addon.py` / `guard_policy.py`. Ports: `8080` (human and
  tools; published to the Mac only as `127.0.0.1:8081` for Burp) and `8082` (Claude; never
  published). For each request it:
  1. requires CONNECT target, TLS SNI and `Host` to agree (`host-mismatch` otherwise),
  2. **checks scope**: out-of-scope wins over in-scope; recon sources are a separate category;
     resolved internal addresses are refused unless the profile lists them,
  3. **rate limits** per program (`rate_limit_rps`; null → 2 req/s; 0 → only human traffic),
  4. **adds the program's identification** (required header, User-Agent template),
  5. **logs every flow**, allowed or refused, with actor (`human`, `claude`, `tool:<name>`) and
     egress IP, as JSONL plus `.mitm` full flows for evidence (CONTRACTS §5).
- Refusals are HTTP 403 with `X-Bounty-Guard: refused; reason=<code>`.
- **Policy lives in tmpfs.** `program use` writes `workspace/bounty/active` on the Mac, then
  `docker exec … bounty-ctl reload` validates the profile and writes a policy snapshot (with the
  username for the identification header) to `/dev/shm/bounty-guard/`. A sync that changes the
  active `program.json` is reloaded automatically. A container restart clears the snapshot, so the
  guard comes back with **no profile** (refuses everything) until `program use` runs again.
- `bounty-run --actor <human|claude> -- <cmd>` runs a command as uid `hunter` in
  `/bounty/work/<handle>` with the proxy environment, adds proxy and actor-header flags for known
  tools (`active-tools`), and picks the guard port by actor. With `--actor claude` it refuses active
  tools when the program forbids automation, and every command when `ai_use` is forbidden.
- v1 is HTTP(S) only. Raw TCP (nmap and the like) is blocked; a SOCKS path with IP scope is later.

### 2.3 Container hardening (ADR 011)

`kali-bounty` runs with `--cap-drop=ALL` plus the eight capabilities the supervisor needs as root
(`NET_ADMIN`, `SETUID`, `SETGID`, `SETPCAP`, `CHOWN`, `FOWNER`, `DAC_OVERRIDE`, `KILL`),
`no-new-privileges`, a **read-only root filesystem** with tmpfs for `/tmp`, `/run`, `/var/log`,
`/etc/ssl/certs` and `/usr/local/share/ca-certificates` (the trust store is rebuilt with the guard
CA on every start), and `--restart unless-stopped`. Guard and hunter run with no capabilities. The
container carries the label `kali-lab.guard-spec`; `guard start` recreates a container with an
older spec and keeps the volumes. Full flags: CONTRACTS §3.3.

### 2.4 Recon (ADR 012)

- **Passive, automatic, Mac side**: `recon passive <handle>` queries crt.sh for each in-scope
  wildcard and resolves names with the Mac's resolver. Nothing goes to the program's own hosts.
  The app runs it after a sync for new in-scope wildcards on the active, followed or top-25
  programs (≤ 3 per sync), and the first time a program is used with `u`.
- **Active, on key press, active program only**: `recon map` (`m`) runs `bounty-map` in the
  container through `bounty-run --actor human`: httpx-toolkit then katana per in-scope host, at the
  program's rate, every request through the guard as `tool:httpx-toolkit` / `tool:katana`.
- `recon list` merges `hosts.jsonl`, `live.jsonl` and `urls.jsonl` into the look-here list, scored
  by `interest` (USER-GUIDE "How interest is scored").

### 2.5 Access-control check (ADR 013)

On demand only. `host/bounty ac session a|b` reads session headers from stdin into the container's
tmpfs (`/dev/shm/bounty-ac/<handle>/`, root 0600). `ac candidates` lists account A's 2xx requests
from the guard's `.mitm` flows; `ac replay` (helper `bounty-ac`) rebuilds chosen requests and sends
each as account B and with no session through the guard as `tool:ac-check`, after dropping to uid
`hunter`. Only GET/HEAD/OPTIONS unless `--unsafe <id>`. Results (`work/<h>/ac/results.jsonl`) hold
statuses, lengths, similarity and verdicts, never bodies or header values.

### 2.6 Work surface: Burp, shell, Claude copilot

- **Burp** stays native (`host/bounty burp`, key `b`): launched with `host/burp/bounty-*.json`,
  upstream proxy `127.0.0.1:8081`, so its traffic is `human` in the flow log.
- **Shell** (`host/bounty shell`, key `t`): a Terminal.app window running `bash -l` as `hunter`
  inside `kali-bounty`, in `/bounty/work/<active handle>`, through the guard.
- **Claude copilot** (`hunter/`, key `h`; ADR 004): Claude Code in Terminal.app with its own
  CLAUDE.md and settings. It reads the program, flows, work dir and Burp history (read-only MCP),
  writes only to `workspace/bounty/findings/`, and runs target traffic only as
  `docker exec kali-bounty bounty-run --actor claude -- <cmd>`, each command approved by you.

### 2.7 The app

`app/src-tauri/src/bounty.rs` and `alerts.rs` spawn `host/bounty` (with Homebrew on `PATH` and
`SOPS_AGE_KEY_FILE` set, since a GUI app lacks the shell's environment) and pass its JSON to the UI
(`programs.ts`, `guard.ts`, `lookhere.ts`). The repo path is fixed at build time
(`CARGO_MANIFEST_DIR/../..`). A timer repeats the sync every 20 min ± 2 min and sends macOS
notifications through `osascript`; the guard status is polled every 5 s while the screen is
visible. Commands and keys: CONTRACTS §9–§12, USER-GUIDE.

## 3. Data: workspace, volumes, tmpfs

| Where | What | Survives |
|---|---|---|
| `workspace/secrets/` (Mac) | SOPS-encrypted tokens; never mounted | everything |
| `workspace/bounty/` (Mac, ro in container) | `config/`, `active`, `sync/`, `activities.jsonl`, `applications.json`, `programs/<h>/` (program.json, raw/, snapshots/, history.jsonl) | everything |
| `workspace/bounty/work/<h>/` (rw in container) | tool output; `recon/` (hosts, live, urls, mapped, state); `ac/results.jsonl` | everything |
| `workspace/bounty/findings/` (Mac) | finding drafts written with the copilot | everything |
| volume `kali-bounty-guard` → `/var/lib/bounty-guard` | `guard.json`, `guard-events.log`, guard CA, `flows/<h>/<date>.jsonl` + `.mitm` | restarts, `guard stop`; removed by `guard stop --purge --yes` |
| volume `kali-bounty-home` → `/home/hunter` | the shell user's home (history, tool config) | restarts, `guard stop`; removed by `guard stop --purge --yes` |
| tmpfs `/dev/shm/bounty-guard/` | active policy snapshot incl. username | **not** a container restart |
| tmpfs `/dev/shm/bounty-ac/<h>/` | access-control sessions and markers | **not** a container restart |
| tmpfs `/tmp`, `/run`, `/var/log`, trust store | runtime files, firewall expectations | **not** a container restart |

Plain files (JSON, JSONL, Markdown) can be grepped and scripted, and the app reads the same files.
Evidence leaves the volume with `host/bounty flows export <handle> <dir>`.

## 4. Tech choices

| Choice | Why |
|---|---|
| Python 3, stdlib only, for `bounty/` | runs on the Mac's python3 with nothing to install; easy to test |
| mitmproxy (`mitmdump` + addon) for the guard | mature, scriptable in Python, HTTP/1, HTTP/2, WebSockets and TLS interception; we write the policy, not a proxy |
| iptables uid-owner lock | makes the guard unavoidable instead of relying on tools respecting proxy env vars |
| tmpfs for policy and sessions | nothing sensitive or policy-defining persists across a restart; fail closed |
| SOPS + age for secrets | diffable encrypted files; one key on the Mac |
| Intigriti Researcher API | structured scope and rules instead of copying pages by hand |
| Native Burp and Terminal.app | performance and familiarity over an embedded UI |
| Files over a database | greppable, scriptable, no migrations; enough for one tester |

## 5. Non-goals (for now)

- Other platforms (HackerOne, Bugcrowd, YesWeHack) and the Lithuanian programs: parked. The profile
  format is platform-neutral so importers can be added.
- Auto-submitting reports, or automatic replays and scans. The human decides and submits.
- Non-HTTP testing (nmap, raw TCP) from `kali-bounty`.
- Distributed or VPS scanning, and scanning from anything except this Mac.
- Windows. Analyze mode.
