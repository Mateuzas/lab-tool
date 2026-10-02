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
