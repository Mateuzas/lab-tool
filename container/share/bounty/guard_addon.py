"""bounty-guard: mitmproxy addon that enforces the active program profile (ADR 005, CONTRACTS §3, §5).

Runs inside mitmdump as uid `guard` (the only uid iptables lets open outbound connections).
Policy comes only from the snapshot bounty-ctl writes to tmpfs (root-owned) at reload time. Flows
go to the guard's own Docker volume /var/lib/bounty-guard/flows/<handle>/, which hunter can neither
read nor write (the /bounty bind mount is read-only; Docker Desktop's file sharing would not
enforce permissions there).

Every request is decided in `request` (CONTRACTS §3.1). `server_connect` is a second, independent
gate: mitmproxy only opens an upstream connection to a host the current policy allows, to the exact
address that was checked (no DNS rebinding between check and connect). Raw TCP/UDP is killed.
"""

import asyncio
import collections
import datetime
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guard_policy as gp  # noqa: E402

from mitmproxy import http  # noqa: E402
from mitmproxy import io as mio  # noqa: E402

SNAPSHOT = os.environ.get("BOUNTY_SNAPSHOT", "/dev/shm/bounty-guard/policy.json")
RUN_DIR = os.environ.get("BOUNTY_RUN_DIR", "/dev/shm/bounty-guard-run")
FLOWS = os.environ.get("BOUNTY_FLOWS", "/var/lib/bounty-guard/flows")
NO_PROFILE = "_none"   # flows refused while no program was active
CLAUDE_PORT = int(os.environ.get("BOUNTY_CLAUDE_PORT", "8082"))
DNS_TTL = 30.0


def now_iso(ms=True):
    d = datetime.datetime.now(datetime.timezone.utc)
    if ms:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _hdr_bytes(headers):
    return sum(len(k) + len(v) + 4 for k, v in headers.items(multi=True))


class Tunnel(object):
    __slots__ = ("host", "port", "actor_header")

    def __init__(self, host, port, actor_header):
        self.host, self.port, self.actor_header = host, port, actor_header


class BountyGuard(object):
    def __init__(self):
        self.policy = None
        self.error = None
        self.handle = None
        self.generation = None
        self.snap_sig = None
        self.target_bucket = None
        self.recon_bucket = None
        self.tunnels = {}
        self.dns = {}
        self.egress_ip = None
        self.egress_error = None
        self.log_error = None
        self.egress_started = False
        self.counts = {"allowed": 0, "refused": 0}
        self.last_refusals = collections.deque(maxlen=5)
        self.started_at = now_iso(False)
        self._status_due = 0.0
        self._lock = threading.Lock()
        self._load_snapshot(force=True)

    # ------------------------------------------------------------------ policy snapshot
    def _sig(self):
        try:
            st = os.stat(SNAPSHOT)
            return (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load_snapshot(self, force=False):
        sig = self._sig()
        if not force and sig == self.snap_sig:
            return
        self.snap_sig = sig
        policy, error, handle, gen = None, None, None, None
        if sig is not None:
            try:
                with open(SNAPSHOT) as f:
                    snap = json.load(f)
                handle = snap.get("handle")
                gen = snap.get("generation")
                if snap.get("error"):
                    error = snap["error"]
                elif handle:
                    policy = gp.Policy(snap.get("profile"), snap.get("username"), snap.get("recon"), handle)
            except gp.PolicyError as e:
                error = e.message
            except Exception as e:  # unreadable snapshot: refuse everything, say why
                error = "cannot read policy snapshot: %s: %s" % (type(e).__name__, e)
        self.policy, self.error, self.handle, self.generation = policy, error, handle, gen
        if policy is not None:
            self.target_bucket = gp.RateLimiter(policy.rps)
            self.recon_bucket = gp.RateLimiter(policy.recon["rate_limit_rps"])
            self.dns.clear()
            if not self.egress_started and policy.recon.get("egress_ip_url"):
                self.egress_started = True
                threading.Thread(target=self._lookup_egress, args=(policy.recon["egress_ip_url"],),
                                 daemon=True).start()
        self._write_status()

    def _lookup_egress(self, url):
        """Public egress IP, once per guard start, from a recon source (CONTRACTS §5). Direct
        request from the guard itself, upstream TLS verified."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kali-lab bounty-guard"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            with opener.open(req, timeout=8) as r:
                text = r.read(200).decode("ascii", "replace").strip()
            ip = gp.parse_ip(text.split()[0] if text else "")
            if ip is None:
                raise ValueError("answer is not an IP address")
            self.egress_ip, self.egress_error = str(ip), None
        except Exception as e:
            self.egress_error = "%s: %s" % (type(e).__name__, e)
        self._write_status()

    def _write_status(self):
        st = {
            "pid": os.getpid(), "started_at": self.started_at, "updated_at": now_iso(False),
            "generation": self.generation, "program": self.handle,
            "status": "active" if self.policy else ("error" if self.error else "no-profile"),
            "error": self.error, "log_error": self.log_error, "egress_ip": self.egress_ip, "egress_error": self.egress_error,
            "counts": dict(self.counts), "last_refusals": list(self.last_refusals),
            "policy": self.policy.describe() if self.policy else None,
        }
        try:
            with self._lock:
                write_json_atomic(os.path.join(RUN_DIR, "status.json"), st)
        except OSError:
            pass

    async def _poll(self):
        while True:
            try:
                self._load_snapshot()
                if time.monotonic() >= self._status_due:
                    self._status_due = time.monotonic() + 2.0
                    self._write_status()
            except Exception:
                pass
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------ mitmproxy hooks
    def running(self):
        asyncio.get_running_loop().create_task(self._poll())

    def client_disconnected(self, client):
        self.tunnels.pop(client.id, None)

    def http_connect(self, flow):
        """Remember the CONNECT target (the first of the three hosts that must agree). Decisions
        happen per request inside the tunnel so the client sees the 403 with its reason."""
        req = flow.request
        self.tunnels[flow.client_conn.id] = Tunnel(req.host, req.port, req.headers.get(gp.ACTOR_HEADER))

    def tcp_start(self, flow):
        flow.kill()   # v1 is HTTP(S) only (ADR 005)

    def udp_start(self, flow):
        flow.kill()

    def dns_request(self, flow):
        flow.kill()

    def _actor(self, flow, tunnel):
        hdr = flow.request.headers.get(gp.ACTOR_HEADER)
        if hdr is None and tunnel is not None:
            hdr = tunnel.actor_header
        sock = flow.client_conn.sockname
        port_actor = "claude" if sock and sock[1] == CLAUDE_PORT else "human"
        return gp.actor_for(port_actor, hdr)

    def _refuse(self, flow, meta, reason, detail=""):
        meta["decision"], meta["reason"] = "refused", reason
        flow.response = http.Response.make(
            403, gp.refusal_body(reason, detail).encode(),
            {"X-Bounty-Guard": "refused; reason=%s" % reason, "Content-Type": "text/plain; charset=utf-8",
             "Connection": "close"})

    async def _resolve(self, host, port):
        if gp.is_ip_literal(host):
            return [gp.norm_host(host)]
        hit = self.dns.get(host)
        if hit and hit[1] > time.monotonic():
            return hit[0]
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.run_in_executor(None, socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        except OSError:
            return None
        ips = []
        for fam, _, _, _, sa in infos:
            ip = gp.norm_host(sa[0])
            if ip not in ips:
                ips.append(ip)
        ips.sort(key=lambda a: 0 if ":" not in a else 1)   # IPv4 first
        self.dns[host] = (ips, time.monotonic() + DNS_TTL)
        return ips

    async def request(self, flow):
        t0 = time.time()
        tunnel = self.tunnels.get(flow.client_conn.id)
        actor = self._actor(flow, tunnel)
        while gp.ACTOR_HEADER in flow.request.headers:
            del flow.request.headers[gp.ACTOR_HEADER]   # never forwarded (CONTRACTS §5)
        meta = flow.metadata.setdefault("bounty", {})
        policy = self.policy
        meta.update({"t0": t0, "actor": actor, "program": policy.handle if policy else self.handle,
                     "category": "target", "decision": "allowed", "reason": None, "ip": None})
        req = flow.request
        authority = tunnel.host if tunnel is not None else req.host
        tls = bool(flow.client_conn.tls_established)
        d = gp.decide(policy, authority, flow.client_conn.sni, req.host_header, tls, req.scheme, req.port,
                      req.path, actor)
        meta["category"], meta["host"] = d.category, d.host
        # mitmproxy connects to request.host:port; it must be the agreed host (and the CONNECT port).
        if d.allowed and (gp.norm_host(req.host) != d.host or (tunnel is not None and tunnel.port != req.port)):
            d = gp.Decision(False, "host-mismatch", d.category, d.host)
        if not d.allowed:
            detail = self.error if (d.reason == "no-profile" and self.error) else ""
            return self._refuse(flow, meta, d.reason, detail)
        ips = await self._resolve(d.host, req.port)
        if ips is None:
            meta["decision"], meta["reason"] = "allowed", None
            meta["error"] = "dns"
            flow.response = http.Response.make(502, b"bounty-guard: cannot resolve host\n",
                                               {"Content-Type": "text/plain"})
            return
        reason = policy.address_decision(ips)
        meta["ip"] = ips[0] if ips else None
        if reason:
            return self._refuse(flow, meta, reason)
        bucket = self.recon_bucket if d.category == "recon-source" else self.target_bucket
        wait = bucket.reserve(time.monotonic())
        if wait is None:
            return self._refuse(flow, meta, "rate-limited")
        if wait > 0:
            await asyncio.sleep(wait)
        if self.policy is not policy:   # reloaded while waiting: decide again with the new profile
            policy = self.policy
            d = gp.decide(policy, authority, flow.client_conn.sni, req.host_header, tls, req.scheme, req.port,
                          req.path, actor, ips)
            if not d.allowed:
                return self._refuse(flow, meta, d.reason)
            meta["program"] = policy.handle
        if d.category == "target":
            for name, value in policy.ident.headers(req.headers.get("User-Agent")).items():
                req.headers[name] = value   # replaces any existing header of that name
        # server_connect pins each upstream connection to the checked IP, so mitmproxy can't match it
        # to the next request's host:port and never reuses it; without this every request left an
        # idle connection open to the target for as long as the client stayed connected (a long
        # Burp session: hundreds), and servers that limit connections stalled. One connection per
        # request, closed by the server after its answer. HTTP/1 only: HTTP/2 has no such header.
        if req.http_version.startswith("HTTP/1"):
            req.headers["Connection"] = "close"

    async def server_connect(self, data):
        """Last gate before any upstream connection: host allowed by the current profile, every
        resolved address allowed, and the connection pinned to the checked address."""
        srv = data.server
        host, port = srv.address
        policy = self.policy
        reason = "no-profile" if policy is None else policy.host_decision(host)[0]
        if not reason:
            ips = await self._resolve(gp.norm_host(host), port)
            if not ips:
                reason = "dns"
            else:
                reason = policy.address_decision(ips)
                if not reason:
                    if srv.sni is None and not gp.is_ip_literal(host):
                        srv.sni = host
                    srv.address = (ips[0], port)
        if reason:
            srv.error = "bounty-guard refused upstream connection: %s" % reason

    def response(self, flow):
        self._log(flow)

    def error(self, flow):
        self._log(flow)

    # ------------------------------------------------------------------ flow log (CONTRACTS §5)
    def _log(self, flow):
        meta = flow.metadata.get("bounty")
        if not meta or meta.get("logged"):
            return
        meta["logged"] = True
        req, resp = flow.request, flow.response
        end = (resp.timestamp_end if resp and resp.timestamp_end else time.time())
        peer = flow.server_conn.peername if flow.server_conn else None
        rec = collections.OrderedDict([
            ("ts", now_iso()), ("program", meta.get("program")), ("actor", meta.get("actor")),
            ("category", meta.get("category")), ("decision", meta.get("decision")),
            ("reason", meta.get("reason")), ("method", req.method), ("url", req.pretty_url),
            ("host", meta.get("host")),
            ("ip", (peer[0] if peer else meta.get("ip"))),
            ("status", resp.status_code if resp else None),
            ("req_bytes", _hdr_bytes(req.headers) + len(req.raw_content or b"")),
            ("resp_bytes", (_hdr_bytes(resp.headers) + len(resp.raw_content or b"")) if resp else 0),
            ("ms", int(round((end - meta.get("t0", end)) * 1000))),
            ("egress_ip", self.egress_ip), ("flow_id", flow.id),
        ])
        if flow.error:
            rec["error"] = str(flow.error.msg)[:200]
        elif meta.get("error"):
            rec["error"] = meta["error"]
        if rec["decision"] == "refused":
            self.counts["refused"] += 1
            self.last_refusals.append({"ts": rec["ts"], "reason": rec["reason"], "actor": rec["actor"],
                                       "method": rec["method"], "url": rec["url"][:300]})
        else:
            self.counts["allowed"] += 1
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        prog = rec["program"]
        base = os.path.join(FLOWS, prog if prog and gp.HANDLE_RE.match(prog) else NO_PROFILE)
        try:
            os.makedirs(base, mode=0o750, exist_ok=True)
            with open(os.path.join(base, day + ".jsonl"), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            with open(os.path.join(base, day + ".mitm"), "ab") as f:
                mio.FlowWriter(f).add(flow)
        except OSError as e:
            self.log_error = "cannot write flow log: %s" % e
        if rec["decision"] == "refused":
            self._write_status()


addons = [BountyGuard()]
