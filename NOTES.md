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
