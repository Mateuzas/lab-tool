# ADR 005: The scope guard is a mitmproxy addon behind a uid-locked firewall

**Status:** accepted. **Date:** 2026-11-13.

**Context.** In Bug bounty there is no VPN boundary: traffic leaves from the user's home IP to real
companies. Going out of scope or over a rate limit can mean a platform ban or criminal liability. Scope
is defined by hostnames and wildcards, not IPs, and programs require a request header and "attack logs".

**Options.** (a) Rely on Burp's scope settings: CLI tools bypass it. (b) DNS-based firewall (resolve
scope to IPs): breaks on CDNs and shared IPs. (c) Our own proxy from scratch: too much to get right.
(d) mitmproxy with a policy addon, made unavoidable by iptables.

**Decision.** (d). In `kali-bounty`, only uid `guard` may open outbound connections; the guard
(mitmdump + addon) checks Host/SNI/CONNECT against the active profile, blocks internal addresses,
rate limits, adds the required header and logs every flow (CONTRACTS §3, §5). Without a profile,
nothing goes out. v1 is HTTP(S) only; raw TCP is blocked.

**Consequences.** + Fails closed, covers Burp and every CLI tool, gives evidence for free. − TLS
interception needs a container CA; non-HTTP testing waits for a later SOCKS path; one more process to
supervise. Tests must prove refusals (out of scope, tool ignoring proxy, internal IP, rate limit)
before the first real request.
