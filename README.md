# kali-lab

Prototype backend for a Mac app that gives HackTheBox and TryHackMe beginners one interface:
a Kali container on the lab VPN, native Burp Suite and Wireshark, and Claude as a tutor.
The product plan is in `../project.md`; results of every gate are in `docs/spike-notes.md`.

## Layout

```
app/                 desktop app (Tauri 2: Rust backend in src-tauri/, TypeScript UI in src/)
  src-tauri/src/lab.rs  talks to the lab container through the Docker API (cross-platform)
container/           everything that runs inside the lab container (baked into the image)
  Dockerfile
  bin/lab-supervisor   main process: keeps kill switch, VPN and proxy up; writes status.json
  bin/lab-kill-switch  egress firewall: lab traffic only through the VPN, only to set targets
  bin/lab-shell        student terminal; logs commands for the tutor
  share/lab/bashrc     the command-log hook
host/                Mac side (the app will replace these)
  lab                  control CLI: build, up, off, status, target, shell, history, logs, capture, burp
  burp/                Burp launch config (SOCKS proxy to the lab)
tests/               gate and fault-injection tests
tools/               dev utilities (mcp-probe.py: Burp MCP; drive-shell.py: types into lab shell)
docs/                spike notes and decisions
tutor/               sandboxed Claude Code tutor (run `claude` here): reads logs + Burp, cannot act
workspace/           USER DATA, gitignored, the only thing mounted into the container (/lab)
  vpn/                 .ovpn files (contain credentials - never commit or share)
  state/               status.json, events.log, selected-vpn
  captures/            .pcap files
  logs/                commands.jsonl (one line per command) + session-*.log transcripts
.mcp.json            Claude Code -> Burp MCP (read-only; write tools denied in .claude/settings.json)
```

Principles: code lives in the image and is immutable at runtime; user data lives only in
`workspace/`. The Mac side sends commands with `docker exec` (never by editing shared files:
Docker Desktop can serve stale copies for 30 s+) and reads results from `workspace/state/`.

## Quick start

Requires Docker Desktop (Apple silicon). Put your HTB `.ovpn` files in `workspace/vpn/`.

```
make build            # build kali-lab:dev
make up VPN=starting  # connect with the file whose name contains "starting"
make status
host/lab target 10.129.x.x  # only the machine you spawned is reachable
host/lab shell        # the student terminal (commands logged to workspace/logs/)
host/lab burp         # Burp, pre-configured to use the lab proxy
host/lab capture live # live lab traffic in Wireshark
make test             # lint + container tests (supervisor, scope, command log)
make app-dev          # run the desktop app (needs Rust: rustup.rs)
make app-test         # UI type-check + Rust unit tests
make test-target TARGET=10.129.x.x   # needs a machine you spawned yourself
```

Only test machines you spawned yourself. Claude reads and teaches; the student runs every command.
