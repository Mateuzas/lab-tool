"""Browse-only hosts (CONTRACTS §3.5): hosts a program's own site needs (its CDN, its login) that
are not in scope. You approve each one; the guard then lets YOUR traffic (Burp, the terminal: actor
human) reach it, read methods only unless approved for any method. Tools, maps, replays and Claude
stay refused. Hosts the program puts out of scope can't be approved.

  program browse [--handle H] [--json]                    approved hosts + candidates from refusals
  program browse allow HOST... [--any-method] [--handle H]
  program browse remove HOST... [--handle H]

Approvals live in programs/<h>/browse.json (written only on the Mac; the copilot can't write
programs/). The guard picks a change up within a few seconds (autoreload).
"""

import json
import os
import sys

from bounty import findings, guard, recon
from bounty.util import BountyError, iso, read_json, utcnow, write_json

FILE = "browse.json"


def path(ws, handle):
    return os.path.join(ws, "programs", handle, FILE)


def load(ws, handle):
    d = read_json(path(ws, handle), {}) or {}
    hosts = d.get("hosts") if isinstance(d, dict) else None
    return hosts if isinstance(hosts, dict) else {}


def candidates(handle, flows_json=None):
    """Hosts YOUR traffic hit and the guard refused as not-in-scope: {host: {count, last, methods}}."""
    if flows_json is None:
        cfg = guard._cfg()
        if not guard.running(cfg["container"]):
            return {}
        buf = _Buf()
        try:
            guard.flows(handle, tail=5000, as_json=True, out=buf)
        except BountyError:
            return {}
        flows_json = buf.text
    try:
        recs = json.loads(flows_json or "[]")
    except ValueError:
        return {}
    out = {}
    for r in recs:
        if r.get("decision") != "refused" or r.get("reason") != "not-in-scope" or r.get("actor") != "human":
            continue
        h = r.get("host") or ""
        if "." not in h:
            continue   # e.g. Burp's own http://burpsuite page
        c = out.setdefault(h, {"count": 0, "last": None, "methods": []})
        c["count"] += 1
        c["last"] = max(c["last"] or "", r.get("ts") or "")
        m = r.get("method")
        if m and m not in c["methods"]:
            c["methods"].append(m)
    return out


class _Buf(object):
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s


def _gp():
    return recon._policy_module()


def list_cmd(ws, handle=None, as_json=False, out=sys.stdout, flows_json=None):
    h = findings.default_handle(handle)
    approved = load(ws, h)
    cands = {k: v for k, v in candidates(h, flows_json).items() if k not in approved}
    if as_json:
        out.write(json.dumps({"handle": h, "approved": approved, "candidates": cands}) + "\n")
        return 0
    out.write("approved browse-only hosts for %s:%s\n" % (h, "" if approved else " none"))
    for k in sorted(approved):
        out.write("  %-40s %s\n" % (k, "any method" if approved[k].get("methods") == "any" else "GET/HEAD/OPTIONS"))
    if cands:
        out.write("refused while you browsed (not in scope) — approve with: program browse allow HOST\n")
        for k, v in sorted(cands.items(), key=lambda kv: -kv[1]["count"]):
            out.write("  %-40s %4d× %s, last %s\n" % (k, v["count"], "/".join(v["methods"]), ((v["last"] or "")[5:16]).replace("T", " ")))
    return 0


def allow(ws, hosts, any_method=False, handle=None, out=sys.stdout):
    h = findings.default_handle(handle)
    profile, policy = recon.load_profile(ws, h)
    gp = _gp()
    cur = load(ws, h)
    for raw in hosts:
        host = gp.norm_host(raw or "")
        if not gp._HOSTNAME.match(host) or gp.is_ip_literal(host):
            raise BountyError("usage", "%r is not a host name" % raw, 64)
        reason, category = policy.host_decision(host)
        if reason == "out-of-scope":
            raise BountyError("usage", "%s is out of scope for %s: it can't be approved" % (host, h), 64)
        if reason is None and category == "target":
            raise BountyError("usage", "%s is already in scope: nothing to approve" % host, 64)
        cur[host] = {"methods": "any" if any_method else "read", "approved_at": iso(utcnow())}
    write_json(path(ws, h), {"hosts": cur})
    out.write("approved for your browsing on %s (%s): %s\n" % (h, "any method" if any_method else "GET/HEAD/OPTIONS",
                                                           ", ".join(hosts)))
    out.write("the guard picks it up within a few seconds\n")
    return 0


def remove(ws, hosts, handle=None, out=sys.stdout):
    h = findings.default_handle(handle)
    cur = load(ws, h)
    gp = _gp()
    gone = [x for x in (gp.norm_host(r or "") for r in hosts) if cur.pop(x, None) is not None]
    write_json(path(ws, h), {"hosts": cur})
    out.write("removed: %s\n" % (", ".join(gone) or "nothing (not approved)"))
    return 0
