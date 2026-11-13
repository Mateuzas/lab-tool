# Architecture

kali-lab is a Mac desktop app (Tauri 2) that drives a Kali container in Docker Desktop. It has three
modes, and only one runs at a time:

| Mode | For | Status | Design goal |
|---|---|---|---|
| **Lab** | HTB / THM beginners | working | ease of use: plain language, no manual steps |
| **Bug bounty** | the owner, hunting real programs | being built (this document) | **speed for an experienced hunter**: keyboard, density, automation |
| **Analyze** | scam messages (links, emails, attachments) | parked | isolation from hostile content |

Lab mode is documented in the README and `docs/spike-notes.md`. This document is mostly about Bug
bounty mode, plus the parts the modes share.

## 1. Shared foundation

- **One image, one container per mode** (ADR 002). `kali-lab:dev` is built once; Lab runs it as
  `kali-lab` (VPN, kill switch), Bug bounty as `kali-bounty` (scope guard). Each has its own
  mounts, network rules and supervisor. Analyze will be a third container profile.
- **The Mac controls containers with `docker exec`**, never by editing shared files (Docker Desktop
  serves stale copies). Results come back as files in the mode's workspace folder.
- **Secrets are SOPS/age encrypted** (ADR 006). Plaintext exists only in a process's environment
  (`sops exec-env`) or in a container's tmpfs, never on disk.
- **Containers are never `--privileged`**; ports publish on `127.0.0.1` only.

## 2. Bug bounty: how it works

```
                        Mac                                        kali-bounty container
 ┌──────────────────────────────────────────┐        ┌───────────────────────────────────────────────┐
 │ host/bounty (Python, stdlib)             │        │  tools (curl, httpx, katana, ffuf, nuclei...) │
 │  programs sync ──HTTPS──► Intigriti API  │        │     │ HTTP(S)_PROXY            other egress ──X│ (iptables:
 │  programs rank / changes / use           │        │     ▼                                          │  only uid
 │      │ writes                            │        │  bounty-guard (mitmproxy addon, uid "guard")  │  "guard" may
 │      ▼                                   │        │   scope check ─ rate limit ─ required header  │  leave)
 │ workspace/bounty/programs/<handle>/ ◄────┼─mount──┼── flow log + evidence            │             │
 │                                          │        │                                   ▼             │
 │ Burp (native) ──upstream proxy──► 127.0.0.1:8081 ─┼──────────────► guard ──────► internet (in scope)│
 │ hunter/ (Claude Code, approval per cmd) ─┼docker exec─► bounty-run <cmd> (runs as uid "hunter")  │
 │ app (Tauri): Programs, Scope, Flows,     │        └───────────────────────────────────────────────┘
 │   Findings screens read the same files   │
 └──────────────────────────────────────────┘
```

### 2.1 Program intake and ranking (Mac side, no target traffic)

- `host/bounty` talks only to the **Intigriti Researcher API** (ADR 009) with the user's personal
  access token from `workspace/secrets/intigriti.enc.yaml`, read via `sops exec-env`.
- `programs sync` stores every visible program as a normalised `program.json` (CONTRACTS §2) plus
  the raw API response, so the normaliser can be re-run without re-fetching.
- `programs rank` scores programs with a transparent, tunable heuristic (ADR 007, CONTRACTS §4):
  expected payout × chance of finding something × low competition, minus friction. Every factor is
  shown, so you can see *why* a program ranks where it does.
- `programs changes` diffs syncs: new programs, new in-scope assets, bounty raises. Being early on a
  new asset is the cheapest competitive edge there is.
- `program use <handle>` makes a program the **active profile**: scope, rate limit, required header,
  automation and AI rules, legal basis (ADR 008). The guard enforces exactly this file.

### 2.2 Scope guard (container side, enforced)

The guard is the only thing in `kali-bounty` that can open connections to the internet (ADR 005):

- iptables `OUTPUT`: default **drop**; allow loopback, DNS to Docker's resolver, and new outbound
  connections only for uid `guard`. A tool that ignores the proxy settings fails closed.
- `bounty-guard` is `mitmdump` with our addon, listening on `127.0.0.1:8080` inside the container and
  published to the Mac at `127.0.0.1:8081` for Burp. It:
  1. **checks scope** on every request (Host header, CONNECT target and TLS SNI must all match the
     active profile; out-of-scope wins over in-scope; resolved IPs in private/loopback/link-local
     ranges are refused unless the profile lists them),
  2. **rate limits** per program (token bucket; profile value, default 5 req/s),
  3. **adds the program's required request header**,
  4. **logs every flow** (CONTRACTS §5) with the actor (`human`, `claude`, `tool:<name>`) and the
     public egress IP, which is what programs ask for as "attack logs" and "IPs used".
- Refusals return HTTP 403 with header `X-Bounty-Guard: refused; reason=<code>` and are logged.
  Without an active profile, everything is refused.
- Passive recon sources (crt.sh, certificate transparency, archive.org, DNS APIs) are a separate
  allow-list category `recon-source`: allowed, logged, never counted as target traffic.
- v1 is HTTP(S) only. Raw TCP (nmap and the like) is blocked; a SOCKS path with IP scope comes later,
  per program, where the rules allow it.

### 2.3 Claude: the hunter copilot (ADR 004)

- `hunter/` is a Claude Code project like `tutor/`, with its own CLAUDE.md and settings.
- It may **read** the program profile, recon output, flow logs and Burp history (Burp MCP, read-only).
- It may **run** commands only via `docker exec kali-bounty bounty-run --actor claude -- <cmd>`, and
  every such call needs the user's approval (Claude Code permission prompt). So its traffic goes
  through the guard, is rate limited and is labelled `claude` in the flow log.
- If the active profile says `automation: forbidden`, `bounty-run --actor claude` refuses active
  tools and Claude falls back to read, suggest and draft.
- Reports drafted with Claude get an "AI usage" section generated from the flow log, as Intigriti's
  Code of Conduct requires. A human verifies every finding before submission.

### 2.4 Data

Plain files per program under `workspace/bounty/` (CONTRACTS §1): JSON for profiles, JSONL for
logs, Markdown for notes and findings. They can be grepped and scripted, and the app reads the same
files. SQLite comes in only if a screen needs queries that files can't answer quickly.

### 2.5 The app

Bug bounty screens come **after** the CLI for the same slice has been used for real (ADR 003).
They are dense, keyboard-first and read the workspace files: Programs (ranked table), Program
(scope, rules, changes), Flows (guard log), Findings. The design rules are in `docs/design.md`'s
Bug bounty section (to be written with the first screen); they deliberately differ from Lab mode.

## 3. Tech choices

| Choice | Why |
|---|---|
| Python 3, stdlib only, for `bounty/` | runs on the Mac's python3 with nothing to install; easy to test; Wander's data pipeline proved the pattern |
| mitmproxy (`mitmdump` + addon) for the guard | mature, scriptable in Python, HTTP/1, HTTP/2, WebSockets and TLS interception; we write the policy, not a proxy |
| iptables uid-owner lock | makes the guard unavoidable instead of relying on tools respecting proxy env vars |
| SOPS + age for secrets | already used in Wander; diffable encrypted files; one key on the Mac |
| Intigriti Researcher API | structured scope and rules instead of copying pages by hand |
| Files over a database | greppable, scriptable, no migrations; enough for one hunter |

## 4. Non-goals (for now)

- Other platforms (HackerOne, Bugcrowd, YesWeHack) and the Lithuanian programs: parked. The profile
  format is platform-neutral so importers can be added.
- Auto-submitting reports. The human submits, always.
- Distributed or VPS scanning, and scanning from anything except this Mac.
- Windows.
- Analyze mode.
