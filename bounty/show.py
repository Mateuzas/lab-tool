"""`program show [H] [--json]` (CONTRACTS §6): one program at a glance — profile, scope, rules,
legal basis, application, and what recon, findings and time say about it. Local files only."""

import json
import os
import sys

from bounty import applications, findings, recon, timelog
from bounty.util import BountyError, read_json


def summary(ws, handle):
    profile = read_json(os.path.join(ws, "programs", handle, "program.json"))
    if profile is None:
        raise BountyError("usage", "no program %s" % handle, 64)
    rules = profile.get("rules") or {}
    scope = profile.get("scope") or {}
    try:
        rr = recon.rows(ws, handle)
    except BountyError:
        rr = []
    ins = [r for r in rr if r["scope"] == "in-scope"]
    t = [r for r in timelog.report_rows(ws, 3650) if r["handle"] == handle]
    app = applications.summary(ws, handle)
    return {
        "handle": handle, "name": profile.get("name"), "platform": profile.get("platform"),
        "url": profile.get("url"), "status": profile.get("status"), "detail": profile.get("detail"),
        "bounty": profile.get("bounty"), "legal_basis": profile.get("legal_basis"),
        "authorization": profile.get("authorization"),
        "rules": {k: rules.get(k) for k in ("rate_limit_rps", "automation", "ai_use", "request_header",
                                              "user_agent", "intigriti_me", "safe_harbour")},
        "scope": {"in": [(e.get("type"), e.get("value")) for e in scope.get("in") or []],
                  "out": [(e.get("type"), e.get("value")) for e in scope.get("out") or []]},
        "application": app,
        "recon": {"hosts": len(ins), "live": sum(1 for r in ins if r["live"]),
                  "new": sum(1 for r in ins if r["new"]),
                  "exposed": sum(1 for r in ins if r.get("exposures"))},
        "findings": findings.rows(ws, handle),
        "time": t[0] if t else {"hours": 0, "payout_eur": 0, "eur_per_hour": None},
    }


def show(ws, handle=None, as_json=False, out=sys.stdout):
    s = summary(ws, findings.default_handle(handle))
    if as_json:
        out.write(json.dumps(s) + "\n")
        return 0
    r, b = s["rules"], s["bounty"] or {}
    w = out.write
    w("%s (%s, %s)\n%s\n" % (s["name"], s["handle"], s["status"], s["url"] or ""))
    if b.get("max"):
        w("bounty: %s–%s %s\n" % (b.get("min"), b.get("max"), b.get("currency") or ""))
    w("rules: %s req/s, automation %s, AI %s%s%s\n" % (
        r["rate_limit_rps"], r["automation"], r["ai_use"],
        (", header " + r["request_header"]) if r["request_header"] else "",
        ", @intigriti.me accounts" if r["intigriti_me"] else ""))
    w("legal basis: %s" % s["legal_basis"])
    a = s["authorization"] or {}
    if s["legal_basis"] == "authorization":
        w(" (owner %s; written note: %s)" % (a.get("owner") or "?", a.get("note_path") or "NOT RECORDED"))
    w("\n")
    if s["application"]:
        w("application: %s since %s\n" % (s["application"]["state"], s["application"]["ts"]))
    w("scope in (%d):\n" % len(s["scope"]["in"]))
    for t, v in s["scope"]["in"]:
        w("  %-9s %s\n" % (t, v))
    if s["scope"]["out"]:
        w("scope out (%d):\n" % len(s["scope"]["out"]))
        for t, v in s["scope"]["out"]:
            w("  %-9s %s\n" % (t, v))
    rc = s["recon"]
    w("recon: %d in-scope hosts, %d live, %d new, %d with exposures\n" % (rc["hosts"], rc["live"], rc["new"], rc["exposed"]))
    w("findings: %s\n" % (", ".join("%s (%s)" % (f["id"], f["status"]) for f in s["findings"]) or "none"))
    tm = s["time"]
    w("time: %.2f h, €%s paid%s\n" % (tm["hours"], tm["payout_eur"],
                                      ", €%s/h" % tm["eur_per_hour"] if tm.get("eur_per_hour") is not None else ""))
    return 0
