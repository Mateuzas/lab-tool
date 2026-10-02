# Spike notes

| Step | Minutes | Prompts or problems |
|---|---|---|
| 1.3 Docker Desktop | 0 (already installed) | Docker 29.3.1, server linux/arm64. Daemon started in ~3 s. |
| 1.4 docker run | 0.5 (29 s incl. 210 MB image pull) | Ran detached (`-dit`) instead of `-it`; same flags. No errors. |
| 1.5 arch + TUN | 0 | `aarch64` (native, no emulation). `/dev/net/tun` is `crw-rw-rw-`. |
| 1.6 base packages | 0.5 (33 s) | 30.3 MB download, 125 MB on disk. OpenVPN 2.7.7, Nmap 7.99. |
| 2.7 clock | 0 | Mac and container `date -u` identical to the second. |
| 3.1 microsocks | 0 | Candidate 1.0.5-4. |
| 3.3 bind | 0 | Listens on `172.17.0.2:1080` only (eth0), not 0.0.0.0. |
| 3.4 Mac side | 0 | `lsof`: `127.0.0.1:1080` only. Connecting via LAN IP 10.38.129.96:1080 refused. |
| 3.5 (pre-VPN) | 0 | curl via proxy to http://example.com -> 200, plumbing works. |

## Findings

- **Route baseline before VPN (Review Focus 2/5):** with no tunnel, `ip route get 10.129.10.10`
  goes `via 172.17.0.1 dev eth0` -> Mac -> home router. Home LAN here is 10.38.129.0/24, so no
  overlap with HTB ranges, but on networks that route all of 10.0.0.0/8 (school/office) a dropped
  tunnel would send lab traffic to real machines. Step 3.6 must confirm failure; likely needs a
  container-side kill switch (iptables egress allow-list) in the tool-server task.
- microsocks is started with `setsid ... &` so it survives `docker exec` returning. Background
  processes do not survive a container restart.

## Task 6 research (2026-10-02)

- **6.1 HTB: disallowed for AI-run actions, allowed for AI explaining.** AUP (eff. 2026-04-01)
  6.1: no "AI agent, large language model ... to directly access, interact with, or extract data
  from any part of the Services". 6.2: no using challenges/solutions/hints/output to train,
  evaluate or test AI. 8.2: no hints or spoilers for *active* content. 11.3(b): on assessments,
  no automated system producing work for you.
- **6.1 THM: same line, stated more clearly.** AUP 9.2 bans AI agents/LLMs accessing the
  platform. 9.5: "Asking an AI assistant ... to explain a concept to you is fine, unless the room
  ... tells you not to"; not fine: "letting it work through the platform for you".
- **Design consequence:** Claude must never send traffic to a target itself. The student types and
  runs every command; Claude only reads output and teaches. The tool server's job becomes
  scope + logging + feeding output to the tutor, not executing on Claude's behalf. Burp MCP
  tools that send requests (e.g. send_http_request, Repeater/Intruder) must be disabled;
  read-only history access only. The tutor must not supply flags/answers for active machines
  (hint ladder stops at approach for active content) and must refuse during exams.
- **6.3 Docker Desktop:** free for personal use, education, non-commercial OSS, and companies
  with <250 employees AND <$10M revenue. Larger companies need Pro/Team/Business. Windows
  needs WSL2. Fine for a hobby/student audience; a blocker for corporate training sales.
- **6.4 Network Extension:** not needed while the VPN runs inside the container.
- **Burp MCP:** PortSwigger's official MCP Server extension (BApp Store) works on Community
  Edition and ships a stdio proxy with auto-install for Claude Desktop.
- **6.2 (who pays) and 6.5 (employment contract): Matas to decide/read.**

## Plan corrections

- **Step 1.6 must also install `procps`.** The kali-rolling base image has no `pkill`, which
  steps 2.4/3.6 use. Corrected package list:
  `apt install -y openvpn iproute2 iputils-ping curl nmap procps microsocks`
- `vpn-up.sh` (2.2-2.8, 3.3-3.4) and `target-test.sh <IP> [PORT]` (2.9, 3.5-3.6) script the
  manual steps. Tested: missing .ovpn -> refused; .ovpn with `up`/`script-security` hooks ->
  refused; malformed .ovpn -> openvpn log shown. Not yet tested against a real HTB server.

## Location change (2026-10-02)

- Repo moved from `~/kali-lab` to `~/Desktop/Projects/kali-lab`. Wherever the plan says
  `~/kali-lab`, read this path. Container recreated with `-v ~/Desktop/Projects/kali-lab:/lab`.
- Packages now come from `Dockerfile` (image `kali-lab:spike`) instead of a manual
  `apt install`, so recreating the container keeps them. Same run flags as step 1.4.

## Gate run 1 (2026-10-02, real HTB VPN)

- **Task 2.2-2.8: PASS.** `vpn-up.sh` brought the tunnel up in 4 s (UDP 1337, edge-eu-free-3).
  tun0 = 10.10.15.46/23; 10.129.0.0/16 and other lab ranges via tun0; 8.8.8.8 via eth0.
  Clocks identical. Hook check clean. Only warnings: deprecated `persist-key` and compression.
- **Task 3.3-3.4: PASS** again with the tunnel up (eth0-only listener, Mac 127.0.0.1 only).
- **Task 2.9: blocked, not a gate failure.** Target 10.129.14.180 -> "Destination Host
  Unreachable" from the HTB gateway 10.10.14.1 (so the tunnel works). The VPN file was for the
  Labs free server, and the machine was on another VPN network. Beginner UX lesson: the app
  must match the VPN file to the section/server the machine was spawned on, or say clearly why not.

## UX finding: VPN file per HTB section (2026-10-02)

- Meow (listed at /machines/Meow) was unreachable on the Machines VPN (EU Machines 3 =
  edge-eu-free-3) while the HTB header showed "Connected". It is a Starting Point box and needs
  the Starting Point VPN (EU StartingPoint 2). Both VPNs push 10.129.0.0/16, so the target IP
  alone cannot tell which file is needed.
- App idea: import each section's .ovpn once, label it from its `remote` hostname, and have the
  student pick the section (or the machine) so the app switches tunnels automatically. Files only
  change if the student changes server. HTB API automation of this is a grey area under AUP
  6.1/6.3, so do not build it without asking HTB.

## Gate run 2 (2026-10-02, Starting Point VPN, target Appointment 10.129.14.194)

| Step | Result |
|---|---|
| 2.2-2.8 | PASS. `./vpn-up.sh starting`, edge-eu-starting-point-2, tun0 10.10.15.248, up in ~4 s. |
| 2.9 | PASS. Ping 90-125 ms, `nmap -F`: 80/tcp open http (1.96 s). |
| 3.5 | PASS. Mac -> 127.0.0.1:1080 -> container -> tun0 -> target: HTTP 200. |
| 3.6 | PASS as written: with openvpn killed, curl via proxy timed out (10 s), no HTTP response. Restored -> 200. |

- **Caveat on 3.6:** a timeout means the SYN *was sent* - out eth0 to the home router - and nothing
  answered. On this LAN that is harmless; on a network that routes 10.0.0.0/8 it would reach
  real hosts. The gate passes, but a kill switch (container egress: lab ranges only via tun0,
  eth0 only to the VPN server) is still required in the tool-server task.
- **Tasks 2 and 3 pass.** Per Global Constraints, Task 4 (Burp) is next.

## Task 4 (2026-10-02, Burp Suite 2026.8.0 Community, "Burp Suite.app" combined installer)

- **4.2 automated: PASS.** Launched with
  `open -a "Burp Suite" --args --user-config-file=burp/user-options.json --config-file=burp/project-options.json`.
  Settings showed SOCKS proxy on, "Configured proxy 127.0.0.1:1080 with DNS tunnelling", with
  "Override options for this project only" on (the project file took effect). No manual setup.
  App consequence: the app can launch Burp pre-configured; beginners never open Settings.
- Install note: the DMG must be dragged to Applications; the app is now named `Burp Suite.app`
  (not "Burp Suite Community Edition.app"). Detection should use the bundle, not the name.
- **4.3: PASS.** Burp's browser loaded http://10.129.14.194/ (Appointment login page). Container
  `ss` showed ESTAB 10.10.15.248 -> 10.129.14.194:80, i.e. Burp traffic left via tun0.
- **4.4: PASS.** Proxy > HTTP history row 1: GET / -> 200, 5125 bytes, HTML, title "Login",
  server Apache/2.4.38 (Debian). IP column reads "unknown host" because DNS goes over SOCKS
  (expected, not an error). Burp's own AI shows "Disabled" in Community.
- **4.5 DECISION: PASS. The Docker architecture is confirmed** (Tasks 2, 3, 4 all pass on M4,
  macOS 26.5.2). Landing-site claims and app work are unblocked. UTM VM stays parked.
- Open before app work: kill switch (3.6 caveat), Task 5 tutor test, Task 6.2 + 6.5 (Matas).

## Claude <-> Burp via MCP (2026-10-02)

- MCP Server BApp 1.3.0 (server "burp-suite" 1.1.2) installed to `~/.BurpSuite/bapps/` (user-level,
  survives temporary projects). Listens on `127.0.0.1:9876` (SSE) only.
- **PASS:** `burp/mcp-probe.py` listed 24 tools and `get_proxy_http_history` returned the
  Appointment request/response (GET / -> 200, Apache/2.4.38, Login page). First history read
  blocked until approved in Burp's UI (the extension's approval system) - good for AUP compliance,
  but the app must surface that prompt or beginners will see a silent hang.
- Request-sending/config-changing tools denied for Claude Code in `.claude/settings.json`
  (send_http1/2, repeater, intruder, set_*_options, intercept, engine, editor writes).
  Allowed: history/organizer/websocket reads, option reads, encode/decode helpers.

## MVP tool list (agreed 2026-10-02)

- Terminal in container (pattern A): nmap, netcat, curl, service clients (FTP/SMB/telnet/SSH/DB),
  web content discovery + wordlists. Burp (B, done). Wireshark (A+B). Burp's browser.
  Notes/editor on `/lab` (C). Later/optional: Metasploit, AD tooling, cracking (native on Mac
  for GPU - Docker on Mac has no GPU), BloodHound-style GUIs. Wordlists live in the image/volume,
  not the bind mount (slow file sharing).
- Rule for every tool: the student runs it; Claude reads logs/history/summaries and teaches.

## Wireshark gate (2026-10-02)

- Image now includes tcpdump 4.99.6 + tshark 4.6.6; size 300 MB -> 623 MB (tshark deps; trim later,
  e.g. summarise on the Mac with native tshark instead).
- **Container side PASS:** `./capture.sh save 8` while loading Appointment via the proxy -> 17
  packets; `./capture.sh summary` shows protocol hierarchy, the TCP conversation
  10.10.15.248 <-> 10.129.14.194:80, and GET / -> 200 OK. That text is what the tutor reads.
- Side observation: target MSS 1362 vs ours 1460, the tunnel shrinks the path MTU. TCP copes via
  MSS; keep the MTU item on the stability list.
- `captures/` is gitignored (captures can contain credentials).
- Pending: live stream into native Wireshark (`./capture.sh live`) once Wireshark is installed.
- **Live stream PASS:** `./capture.sh live` (docker exec tcpdump -U -w - on tun0 | Wireshark -k -i -).
  Wireshark 4.6.9 (Developer ID: Wireshark Foundation 7Z6EMTD2C6) showed Burp's page load live:
  SYN/SYN-ACK, GET / HTTP/1.1, HTTP/1.1 200 OK (text/html), FIN. ChmodBPF and PATH packages
  not installed and not needed (Wireshark's own "no interfaces" warning is expected).
- **Wireshark gate PASS.** All MVP tools now proven: terminal, Burp (+MCP read), Wireshark (+tshark text).

## Onboarding notes for the app

- Both Burp and Wireshark were "downloaded but not installed": the user stopped at the DMG.
  App should offer one Install button per tool: download from the official URL, verify the
  Developer ID signature (codesign), copy the .app into /Applications, detach the DMG.
  This installs from the official source; it is not redistribution.
- Burp's MCP approval prompt and Wireshark's "no interfaces" warning both look like errors to a
  beginner; the app should pre-empt or explain them.

## Step 2.1: Kill switch (2026-10-02)

- `kill-switch.sh` (in container, iptables + ip6tables OUTPUT): allow lo, tun0, eth0
  ESTABLISHED/RELATED (proxy replies to the Mac), DNS to Docker's resolver, the VPN server IP on
  its proto/port from the .ovpn; REJECT everything else (fast failure). IPv6: tun0 only.
  `vpn-up.sh` turns it on before starting openvpn. `off` exists for maintenance (apt).
- `kill-switch-test.sh 10.129.14.194 80` (Appointment):

| Check | Kill switch ON | OFF (control) |
|---|---|---|
| Target via proxy, VPN up | 200 | 200 |
| Internet (example.com) via proxy | blocked | **reachable** |
| VPN dropped: request | fails in 1.09 s | times out after 10 s |
| VPN dropped: lab packets on eth0 | **0** | **11 leaked** |
| VPN reconnects | 200 | 200 |

- **Review Focus 2 fixed and proven.** The original 3.6 "pass" (timeout) hid a real leak: 11
  packets for 10.129.14.194 went to the home router. With the kill switch, none.
- **Review Focus 5 partly addressed:** non-lab destinations are now blocked at the container.
  Per-target scope (only the spawned machine) is still to do in the tool server.
- Known limits: VPN server IP is resolved once at switch-on (a server IP change needs a re-run
  of vpn-up). DNS queries still reach Docker's resolver (names leak, packets don't).

## Step 2.2: Supervisor (2026-10-02)

- `supervisor.sh` is the container's main process (`CMD`, run with `--init --restart unless-stopped`).
  Every 3 s it ensures kill switch, proxy and VPN; health = ping to the HTB VPN gateway on tun0.
  Writes `state/status.json` (state, plain-language message, server, lab IP, gateway ms, proxy,
  kill switch, restarts) and `state/events.log`. The Mac side only writes `state/selected-vpn`.
- States: disconnected / connecting / connected / reconnecting / offline / error.
  Error messages are mapped from OpenVPN logs (AUTH_FAILED, bad file, TUN, DNS, TLS). Network
  errors on a file that has worked before show "Reconnecting...", not "change your setup".
- HTB's pushed ping-restart 120 overridden to ping 5 / ping-restart 20.
- Kill switch gains `block` mode: used when disconnected or when the VPN server can't be
  resolved, so a failure never leaves egress open.
- `./vpn-up.sh starting` from a fresh container: connected in 5 s.
- `supervisor-test.sh` (no target machine needed), all PASS:

| Fault | Result |
|---|---|
| `docker restart` | connected again in 4 s, no script run |
| openvpn `kill -9` | back in 6 s, restart counter 0 -> 1 |
| microsocks killed | restarted within 5 s |
| kill switch flushed | re-applied within 5 s |
| UDP to VPN server dropped (Wi-Fi loss) | "Reconnecting..." after 35 s; connected 3 s after the network returned |
| `vpn-up.sh off` | disconnected, egress fully blocked; reconnect works |
| .ovpn with `up`/`script-security` | refused with a plain-language error |

- First test version passed on a stale status.json (restart "0 s", counter 0 -> 0). Fixed: the test
  only accepts states entered after the fault. Keep this pattern for future tests.
- Tuning later: outage detection (35 s) could be faster; `kill-switch-test.sh` now uses
  `state/hold` and must be re-run when a target is spawned again.
