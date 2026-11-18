"""Two-account access-control check, Mac side (CONTRACTS §13, BE-19).

  ac session a|b [--marker T]... [--clear]   header lines on stdin -> the container's tmpfs
  ac status [--json]                         which sessions are set (names only)
  ac candidates [--since H] [--limit N] [--json]
  ac replay ID... [--unsafe ID]...           B + no-session replays through the guard
  ac results [--json]

Everything runs for the guard's active program. Session values travel only on stdin (into
`docker exec -i`), are kept in the container's tmpfs and are never printed or written to disk.
"""

import json
import os
import subprocess
import sys

from bounty import guard, recon, shell
from bounty.util import BountyError

HELPER = "bounty-ac"


def _active(ws):
    cfg = guard._cfg()
    if not guard.running(cfg["container"]):
        raise BountyError("guard", "%s is not running: start the guard first" % cfg["container"], 3)
    handle = shell.active_program(cfg)
    if not handle:
        raise BountyError("no-program", "the guard has no active program: pick one first (program use)", 5)
    return cfg, handle


def _helper(cfg, handle, sub, extra=(), stdin_text=None):
    args = ["exec"] + (["-i"] if stdin_text is not None else []) + [cfg["container"], HELPER, sub,
                                                                     "--handle", handle] + list(extra)
    code, out, err = guard._docker(args, input_text=stdin_text)
    if code != 0:
        line = next((l for l in reversed(err.splitlines()) if l.startswith("bounty: ")), "")
        msg = line.split(": ", 2)[-1] if line else "bounty-ac %s failed (%d)" % (sub, code)
        raise BountyError("usage" if code == 64 else "ac", msg, 64 if code == 64 else (6 if code == 6 else 1))
    return out


def session(ws, who, markers=None, clear=False, stdin=None, out=sys.stdout):
    cfg, handle = _active(ws)
    if clear:
        _helper(cfg, handle, "session-clear", ["--who", who])
        out.write("cleared account %s for %s\n" % (who.upper(), handle))
        return 0
    stdin = stdin if stdin is not None else sys.stdin
    if hasattr(stdin, "isatty") and stdin.isatty():
        out.write("paste account %s's header lines (Cookie: …, Authorization: …), then Ctrl-D\n" % who.upper())
    text = stdin.read()
    payload = json.dumps({"headers": text, "markers": list(markers or [])})
    res = json.loads(_helper(cfg, handle, "session-set", ["--who", who], stdin_text=payload) or "{}")
    out.write("account %s set for %s: %s%s\n" % (
        who.upper(), handle, ", ".join(res.get("names") or []),
        (", %d marker(s)" % res["markers"]) if res.get("markers") else ""))
    return 0


def status(ws, as_json=False, out=sys.stdout):
    cfg, handle = _active(ws)
    st = json.loads(_helper(cfg, handle, "status") or "{}")
    if as_json:
        out.write(json.dumps(dict(st, handle=handle)) + "\n")
        return 0
    for who in ("a", "b"):
        s = st.get(who)
        out.write("account %s: %s\n" % (who.upper(), "not set" if not s else "%s (%d marker(s), set %s)" % (
            ", ".join(s["names"]), s["markers"], s["set_at"])))
    return 0


def candidates(ws, since=24.0, limit=50, as_json=False, out=sys.stdout):
    cfg, handle = _active(ws)
    rows = json.loads(_helper(cfg, handle, "candidates", ["--since", str(since), "--limit", str(int(limit))]) or "[]")
    if as_json:
        out.write(json.dumps(rows) + "\n")
        return 0
    if not rows:
        out.write("no requests made as account A in the last %g h (browse as A through the guard first)\n" % since)
        return 0
    for r in rows:
        out.write("%-34s %-7s %s %s%s\n" % (r["flow_id"], r["method"], r["status"], r["url"],
                                           "" if r["safe"] else "   (changes data: needs --unsafe)"))
    return 0


def replay(ws, ids, unsafe=None, run=subprocess.run, out=sys.stdout):
    cfg, handle = _active(ws)
    profile, _ = recon.load_profile(ws, handle)
    if not recon.automation_allowed(profile):
        raise BountyError("automation-forbidden",
                          "%s forbids automated tools: compare the two accounts by hand in Burp" % handle, 6)
    if not ids:
        raise BountyError("usage", "give the flow ids to replay (ac candidates lists them)", 64)
    guard._docker(shell.mkdir_args(cfg["container"], handle), check=True)
    args = ["docker", "exec", cfg["container"], HELPER, "replay", "--handle", handle, "--ids", ",".join(ids)]
    if unsafe:
        args += ["--unsafe", ",".join(unsafe)]
    r = run(args, stdout=subprocess.PIPE)          # progress lines pass through on stderr
    if r.returncode != 0:
        return r.returncode
    _print_results(json.loads((r.stdout or b"[]").decode() or "[]"), out)
    return 0


def _flag(r):
    return {"likely-broken-access": 0, "public": 1}.get(r.get("hint"), 2)


def _print_results(rows, out):
    for r in sorted(rows, key=_flag):
        b, n = r.get("b") or {}, r.get("none") or {}
        mark = {"likely-broken-access": "CHECK", "public": "public"}.get(r.get("hint"), "")
        out.write("%-6s %-7s %s\n       A %s  B %s %s (%.2f)  none %s %s\n" % (
            mark, r.get("method"), r.get("url"), (r.get("a") or {}).get("status"),
            b.get("status"), b.get("verdict"), b.get("similarity") or 0, n.get("status"), n.get("verdict")))
    if any(r.get("hint") == "likely-broken-access" for r in rows):
        out.write("CHECK rows: confirm by hand in Burp before reporting (repeat as B, compare the data).\n")


def results(ws, handle=None, as_json=False, out=sys.stdout):
    if not handle:
        _cfg, handle = _active(ws)
    path = os.path.join(ws, "work", handle, "ac", "results.jsonl")
    rows = recon._read_jsonl(path)
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    rows.sort(key=_flag)
    if as_json:
        out.write(json.dumps(rows) + "\n")
        return 0
    if not rows:
        out.write("no replays yet for %s\n" % handle)
        return 0
    _print_results(rows, out)
    return 0
