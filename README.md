# kali-lab

A Mac app that gives HackTheBox and TryHackMe beginners one place to work: a Kali container on the
lab VPN (with a kill switch), your own Burp Suite and Wireshark wired to it, and Claude as a
read-only tutor. The product plan is in `../project.md`; the result of every gate and decision is
in `docs/spike-notes.md`; UI decisions are in `docs/design.md`.

The app has two modes: **Lab** (working today) and **Bug bounty** (a placeholder screen, no
backend yet). Only one runs at a time; launching one stops the other.

## What you need

| Tool | For |
|---|---|
| **Docker Desktop, running** | the lab container. Every `make` target that touches the lab fails without it (`failed to connect to the docker API ... docker.sock`): open Docker Desktop and wait for the whale icon to settle. |
| Node.js 20+ and npm | the app's UI |
| Rust (rustup.rs) | the app's backend (`make app-dev`, `make app-test`) |
| Burp Suite (Community is fine) in `/Applications` | web testing; needs the PortSwigger MCP extension for the tutor to read its history |
| Wireshark in `/Applications` | live view of lab traffic |
| Claude Code (`claude`) | the tutor |
| Your HTB / THM `.ovpn` files | put them in `workspace/vpn/`, or add them from the app |

Apple silicon Mac. Windows is a later goal, not supported yet.

## Quick start

```
open -a Docker           # Docker Desktop must be running first
make build               # build the lab image (kali-lab:dev) and recreate the container
make app-dev             # run the desktop app (first run installs npm packages)
```

In the app: **Lab → add a VPN file → Connect → enter the IP of the machine you spawned → Set
target**. Then use the Terminal, Burp, Wireshark and Claude tabs.

`make build` recreates the container, so run it again after pulling changes to anything in
`container/`. The supervisor reconnects the last VPN and target on its own.

Without the app, the same things work from the terminal (Mac side; the app replaces these):

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

## Make targets

`make help` lists them. The ones you will use:

| Target | What it does |
|---|---|
| `build` | build the image, recreate the container |
| `up VPN=name` / `down` / `status` | connect, disconnect, show status |
| `lint` | ShellCheck on every script (runs in Docker, nothing to install) |
| `test` | lint + tests that need the lab connected but no target machine |
| `test-target TARGET=ip` | kill switch and proxy tests; **only** against a machine you spawned |
| `app-dev` / `app-test` / `app-build` | run the app, type-check + Rust tests, build the `.app` / `.dmg` |
| `app-shots` | screenshots of every UI state (needs the dev server running) |
| `clean` | remove the container (your `workspace/` is kept) |

## Layout

```
app/                    desktop app (Tauri 2: Rust backend in src-tauri/, TypeScript UI in src/)
  src-tauri/src/lab.rs     talks to the lab container through the Docker API (VPN, target, start/stop)
  src-tauri/src/term.rs    the in-app terminals (a shell in the container, streamed to xterm.js)
  src-tauri/src/tools.rs   finds and launches Burp, Wireshark and the Claude tutor
  src/main.ts              the Lab screen; modes.ts the Lab / Bug bounty switcher; terminal.ts the terminals
  src/dev/preview-mock.ts  browser-only mock of the backend, for UI work and screenshots
container/              everything that runs inside the lab container (baked into the image)
  Dockerfile
  bin/lab-supervisor       main process: keeps kill switch, VPN and proxy up; writes status.json;
                           ends sessions to a machine that stops being your target
  bin/lab-kill-switch      egress firewall: lab traffic only through the VPN, only to your targets
  bin/lab-shell            student terminal; logs commands and a full transcript for the tutor
  share/lab/bashrc         the command-log hook
host/                   Mac side
  lab                      control CLI (see above)
  burp/                    Burp launch config (SOCKS proxy to the lab)
tutor/                  the sandboxed Claude Code tutor (the app's Claude tab opens it here)
  CLAUDE.md                teaching rules and what it may see
  .claude/settings.json    read-only permissions; a hook adds a live view of you to every message
  .mcp.json                Burp MCP (read-only tools allowed, request-sending tools denied)
tests/                  fault-injection and gate tests (shell)
tools/                  dev utilities (mcp-probe.py: Burp MCP; drive-shell.py: types into the lab shell)
docs/                   spike-notes.md (gates, decisions, CI and release plan), design.md (UI)
.github/workflows/      CI: lint, app build + Rust tests, container build, and one "CI passed" check
workspace/              YOUR DATA, gitignored, the only folder mounted into the container (/lab)
  vpn/                     .ovpn files (contain credentials: never commit, paste or share)
  state/                   status.json, events.log, selected-vpn, scope
  captures/                .pcap files
  logs/                    commands.jsonl + session-*.log transcripts
```

The root `.mcp.json` and `.claude/settings.json` are older copies of the tutor's; the ones in
`tutor/` are the ones used.

## Principles

- Code lives in the image and is immutable at runtime; user data lives only in `workspace/`.
- The Mac side controls the lab with `docker exec` (never by editing shared files: Docker Desktop
  can serve a stale copy for 30 s or more) and reads results from `workspace/state/`.
- The proxy is published on `127.0.0.1` only. The container is never `--privileged`.
- Traffic leaves the container only through the VPN, and only to the gateway and the targets you set.
  If the VPN drops, everything is blocked, not leaked.

## The tutor

Claude is a teacher, not a hacker. HTB (§6.1) and THM (§9.2) forbid AI acting on their machines, so:
the student runs every command; the tutor can read logs, the lab status and Burp's history, and run
only `host/lab doctor`. It gets a live view of what you have open and typed with every message. Do
not add tools that let it send traffic or run commands against a target.

## Troubleshooting

- **`failed to connect to the docker API` / `docker.sock: no such file`**: Docker Desktop isn't
  running. Start it and retry.
- **`cli.logError is not a function` from `make app-dev`**: `app/node_modules` is corrupt (seen with
  iCloud-synced folders). Run `rm -rf app/node_modules && cd app && npm ci`.
- **A command is missing in the lab terminal** (for example `ssh`): the image is older than the
  repo. `make build`, then `host/lab doctor` to check.
- **The VPN connects but the target doesn't answer**: you used the wrong VPN section's file (Starting
  Point and Machines need different files; the app shows which one is selected) or the machine isn't
  running. On HTB's free tier, stop machines when you finish.

Only test machines you spawned yourself.
