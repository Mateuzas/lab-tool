# kali-lab

Prototype backend for a Mac app that gives HackTheBox and TryHackMe beginners one interface:
a Kali container on the lab VPN, native Burp Suite and Wireshark, and Claude as a tutor.
The product plan is in `../project.md`; results of every gate are in `docs/spike-notes.md`.

## Layout

```
container/           everything that runs inside the lab container (baked into the image)
  Dockerfile
  bin/lab-supervisor   main process: keeps kill switch, VPN and proxy up; writes status.json
  bin/lab-kill-switch  egress firewall: lab traffic only through the VPN
host/                Mac side (the app will replace these)
  lab                  control CLI: build, up, off, status, shell, logs, capture, burp
  burp/                Burp launch config (SOCKS proxy to the lab)
tests/               gate and fault-injection tests
tools/               dev utilities (mcp-probe.py talks to Burp's MCP server)
docs/                spike notes and decisions
workspace/           USER DATA, gitignored, the only thing mounted into the container (/lab)
  vpn/                 .ovpn files (contain credentials - never commit or share)
  state/               status.json, events.log, selected-vpn
  captures/            .pcap files
.mcp.json            Claude Code -> Burp MCP (read-only; write tools denied in .claude/settings.json)
```

Principles: code lives in the image and is immutable at runtime; user data lives only in
`workspace/`. The Mac side talks to the container through `workspace/state/` files.

## Quick start

Requires Docker Desktop (Apple silicon). Put your HTB `.ovpn` files in `workspace/vpn/`.

```
make build            # build kali-lab:dev
make up VPN=starting  # connect with the file whose name contains "starting"
make status
host/lab burp         # Burp, pre-configured to use the lab proxy
host/lab capture live # live lab traffic in Wireshark
make test             # lint + supervisor fault-injection test
make test-target TARGET=10.129.x.x   # needs a machine you spawned yourself
```

Only test machines you spawned yourself. Claude reads and teaches; the student runs every command.
