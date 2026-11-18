# kali-lab

A Mac app (Tauri 2, installed as **Lab Tool**) that drives a Kali container in Docker Desktop. It
has three modes; only one runs at a time, and starting one stops the other.

| Mode | For | Status |
|---|---|---|
| **Lab** | HackTheBox / TryHackMe beginners: a Kali container on the lab VPN with a kill switch, your own Burp and Wireshark, Claude as a read-only tutor | working |
| **Bug bounty** | an experienced tester on Intigriti programs and on targets whose owner authorised you directly: program ranking, a scope guard every request goes through, Burp, a shell and a Claude copilot behind it, recon and a two-account access-control check | working, being extended |
| **Analyze** | scam messages (links, emails, attachments) in isolation | parked |

How it is built: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). How to use Bug bounty mode day to day:
[docs/USER-GUIDE.md](docs/USER-GUIDE.md). Decisions: [docs/adr/](docs/adr/). Secrets:
[docs/SECRETS.md](docs/SECRETS.md). Exact interfaces: [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Requirements

Apple silicon Mac. Windows is a later goal.

| Tool | For |
|---|---|
| **Docker Desktop, running** | both containers. Anything that touches them fails without it (`failed to connect to the docker API ... docker.sock`). |
| Node.js 20+ and npm, Rust (rustup.rs) | building and running the app |
| Python 3 (the Mac's own is fine) | `host/bounty`; stdlib only, nothing to install |
| `sops` and `age` (`brew install sops age`) | the Intigriti token and other secrets |
| Burp Suite (Community is fine) in `/Applications` | web testing in both modes; the PortSwigger MCP extension lets the tutor and the copilot read its history |
| Wireshark in `/Applications` | Lab mode traffic view |
| Claude Code (`claude`) | the Lab tutor and the Bug bounty copilot |
| HTB / THM `.ovpn` files | Lab mode; put them in `workspace/vpn/` or add them from the app |
| An Intigriti personal access token | Bug bounty mode; stored with `scripts/secrets.sh edit intigriti` |

Keep the clone at `~/Developer/kali-lab`, never in an iCloud-synced folder such as `~/Desktop`:
git, Docker file sharing and gitleaks fail there. The built app runs the CLIs and reads `workspace/`
from the path of the clone it was built from, so don't move the clone after installing.

## Install

```
open -a Docker           # Docker Desktop must be running
make build               # build the image kali-lab:dev (both modes use it)
make app-install         # build the app and copy "Lab Tool.app" to /Applications
```

`make app-dev` runs the app with hot reload instead. Run `make build` again after pulling changes
to anything in `container/`. For Bug bounty, `host/bounty guard start` (or `u` in the app) then
recreates `kali-bounty` when its hardening spec is older; volumes are kept.

## Quick start: Lab

In the app: **Lab → add a VPN file → Connect → enter the IP of the machine you spawned → Set
target**. Then use the Terminal, Burp, Wireshark and Claude tabs.

Without the app (Mac side):

```
make up VPN=starting          # connect with the .ovpn whose name contains "starting"
make status
host/lab target 10.129.x.x    # only the machine you spawned is reachable (host/lab target clear)
host/lab shell                # student terminal; commands are logged to workspace/logs/
host/lab history              # last commands typed
host/lab burp                 # Burp, pre-configured to use the lab proxy
host/lab capture live         # live lab traffic in Wireshark
host/lab doctor               # read-only health report of Docker, the lab and the tools
host/lab snapshot             # what the tutor sees of you right now
make down                     # disconnect (everything stays blocked)
```

Run the scripts by path (`host/lab shell`), not as `run host/lab shell`.

## Quick start: Bug bounty

```
scripts/secrets.sh edit intigriti     # once: INTIGRITI_TOKEN and INTIGRITI_USERNAME
```

Open the app; Bug bounty is the default mode. It syncs programs at start and every ~20 min and
shows them ranked. Select a program and press **`u`** to load it into the scope guard, then **`b`**
for Burp, **`t`** for a shell, **`h`** for the Claude copilot, **`l`** for the look-here host list.
Every request from those goes through the guard, which refuses anything out of scope.

The same from the terminal:

```
scripts/secrets.sh exec intigriti -- host/bounty programs sync
host/bounty programs rank --top 20
host/bounty guard start
scripts/secrets.sh exec intigriti -- host/bounty program use intigriti-acme
host/bounty burp                      # Burp with the guard as upstream proxy (127.0.0.1:8081)
host/bounty shell                     # shell in kali-bounty, every request through the guard
host/bounty flows --tail 20           # what the guard allowed and refused
```

Every key and command, the access-control check, manual targets and troubleshooting are in
[docs/USER-GUIDE.md](docs/USER-GUIDE.md). `host/bounty <group> --help` lists the options.

## Make targets

`make help` lists them. The ones you will use:

| Target | What it does |
|---|---|
| `build` | build the image, recreate the Lab container |
| `up VPN=name` / `down` / `status` | Lab: connect, disconnect, show status |
| `lint` | ShellCheck on every script (runs in Docker, nothing to install) |
| `secrets-check` | what the Secrets workflow runs: SOPS files, plaintext, gitleaks |
| `test` | lint + Lab tests that need the lab connected but no target machine |
| `test-target TARGET=ip` | Lab kill switch and proxy tests; **only** against a machine you spawned |
| `app-dev` / `app-test` / `app-build` / `app-install` | run the app, type-check + Rust tests, build the `.app` / `.dmg`, build and copy to /Applications |
| `app-shots` | screenshots of every UI state (needs the dev server running) |
| `clean` | remove the Lab container (your `workspace/` is kept) |

Bug bounty tests (not make targets): `python3 -m unittest discover bounty` (CLI, no network) and
`tests/bounty-guard-test.sh` (the scope guard end to end against local listeners only; needs Docker,
never touches `kali-bounty` or port 8081).

## Layout

```
app/                Tauri 2 app: Rust backend in src-tauri/src/, TypeScript UI in src/
  src-tauri/src/      lab.rs, term.rs, tools.rs (Lab); bounty.rs, alerts.rs (Bug bounty); lib.rs (commands)
  src/                main.ts, terminal.ts (Lab); modes.ts (mode switch); programs.ts, guard.ts, lookhere.ts (Bug bounty)
  src/dev/            browser-only mock of the backend, for UI work and screenshots
container/          the Kali image both modes run from (kali-lab:dev)
  bin/lab-*           Lab: supervisor, kill switch, student shell
  bin/bounty-*        Bug bounty: supervisor, guard, firewall, ctl, run, map, ac
  share/bounty/       guard policy and addon, access-control core, active-tools list, recon sources, bashrc
host/               Mac-side CLIs: lab (Lab), bounty (Bug bounty); burp/ Burp launch configs for both
bounty/             Python package behind host/bounty (stdlib only); unit tests in bounty/tests/
tutor/              Claude Code tutor for Lab (read-only)
hunter/             Claude Code copilot for Bug bounty (approval per command, traffic only via the guard)
scripts/            secrets.sh, check-secrets.sh, secret templates
tests/              shell gate and fault-injection tests (Lab), bounty-guard-test.sh (Bug bounty)
tools/              dev utilities (mcp-probe.py: Burp MCP; drive-shell.py: types into the lab shell)
docs/               architecture, contracts, tasks, ADRs, user guide, secrets, design notes, history
workspace/          YOUR DATA, gitignored
  vpn/ state/ captures/ logs/   Lab (mounted into kali-lab at /lab)
  bounty/                       Bug bounty (read-only in kali-bounty; only bounty/work/ is writable there)
  secrets/                      SOPS-encrypted tokens, never mounted into a container
```

The root `.mcp.json` and `.claude/settings.json` are older copies of the tutor's; the ones in
`tutor/` and `hunter/` are the ones used.

## Principles

- Code lives in the image and is immutable at runtime; user data lives only in `workspace/`.
- The Mac controls containers with `docker exec`, never by editing shared files (Docker Desktop
  can serve a stale copy for 30 s or more).
- Containers are never `--privileged`; published ports bind to `127.0.0.1` only.
- Lab: traffic leaves only through the VPN, and only to the gateway and the targets you set. If the
  VPN drops, everything is blocked.
- Bug bounty: only the scope guard may open outbound connections from `kali-bounty`. With no
  active program, everything is refused.
- Secrets only through SOPS/age; plaintext only in a process's environment or a container's tmpfs.

## Claude in each mode

- **Lab (tutor, `tutor/`)**: a teacher, not a hacker. HTB (§6.1) and THM (§9.2) forbid AI acting on
  their machines, so the student runs every command; the tutor reads logs, the lab status and Burp's
  history, and runs only `host/lab doctor`. Don't add tools that let it send traffic.
- **Bug bounty (copilot, `hunter/`)**: reads the active program, flows, recon output and Burp
  history, drafts findings, and sends traffic only as
  `docker exec kali-bounty bounty-run --actor claude -- <cmd>`, each command approved by you,
  through the guard and labelled `claude` in the flow log.

## Troubleshooting

- **`failed to connect to the docker API` / `docker.sock: no such file`**: Docker Desktop isn't
  running. Start it and retry.
- **`cli.logError is not a function` from `make app-dev`**: `app/node_modules` is corrupt (seen in
  iCloud-synced folders). `rm -rf app/node_modules && cd app && npm ci`.
- **A command is missing in a container** (for example `ssh`): the image is older than the repo.
  `make build`, then `host/lab doctor` (Lab) or `host/bounty guard start` (Bug bounty).
- **Lab: the VPN connects but the target doesn't answer**: wrong VPN file for that section (Starting
  Point and Machines need different files) or the machine isn't running. On HTB's free tier, stop
  machines when you finish.
- **Bug bounty: everything is refused after Docker restarted**: the guard's policy lives in tmpfs;
  load the program again with `u`. More in [docs/USER-GUIDE.md](docs/USER-GUIDE.md#troubleshooting).

Only test machines you spawned yourself, programs whose rules you have read, and systems whose
owner authorised you in writing.
