"""host/bounty entry point (CONTRACTS §6). Errors: one stderr line `bounty: <code>: <message>`."""

import argparse
import json
import os
import sys

from bounty import applications
from bounty import changes as changes_mod
from bounty import guard as guard_mod
from bounty import manual as manual_mod
from bounty import rank as rank_mod
from bounty import recon as recon_mod
from bounty import shell as shell_mod
from bounty import sync as sync_mod
from bounty.config import load_ranking
from bounty.intigriti import Client
from bounty.store import Store
from bounty.util import BountyError, parse_time, utcnow, workspace_dir

TOKEN_ENV = "INTIGRITI_TOKEN"


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # exit 2 is "auth" in CONTRACTS §6; usage errors get 64 (EX_USAGE)
        self.print_usage(sys.stderr)
        sys.stderr.write("bounty: usage: %s\n" % message)
        sys.exit(64)


def build_parser():
    p = _Parser(prog="host/bounty", description="kali-lab bug bounty CLI (Intigriti).")
    sub = p.add_subparsers(dest="group")
    progs = sub.add_parser("programs", help="program intake and ranking")
    ps = progs.add_subparsers(dest="cmd")
    s = ps.add_parser("sync", help="list + detail for open programs + new activities")
    s.add_argument("--handle", help="sync one program (handle, API handle or id); always fetches its detail")
    s.add_argument("--full", action="store_true",
                   help="fetch every open program's detail (default: only new/changed/stale ones)")
    st = ps.add_parser("status", help="last sync time and program counts")
    st.add_argument("--json", action="store_true")
    lst = ps.add_parser("list", help="table of synced programs")
    lst.add_argument("--filter", default=None,
                     help="case-insensitive text matched against handle, name, industry, status, "
                          "confidentiality and detail")
    r = ps.add_parser("rank", help="rank open programs (CONTRACTS §4)")
    r.add_argument("--json", action="store_true")
    r.add_argument("--top", type=int, default=None)
    r.add_argument("--explain", metavar="HANDLE")
    r.add_argument("--include-no-bounty", action="store_true")
    r.add_argument("--include-join", action="store_true")
    c = ps.add_parser("changes", help="diff of the last two syncs -> history.jsonl")
    c.add_argument("--since", help="compare with the state at this time (YYYYMMDDTHHMMSSZ or ISO 8601)")
    c.add_argument("--json", action="store_true",
                   help='print [{"ts","handle","kind","detail"}] (exit 1 and [] when nothing changed)')
    g = sub.add_parser("guard", help="the scope guard container kali-bounty (CONTRACTS §3.3)")
    gs = g.add_subparsers(dest="cmd")
    gs.add_parser("start", help="start kali-bounty (stops kali-lab: one mode at a time)")
    gsp = gs.add_parser("stop", help="remove kali-bounty (workspace and the flows volume are kept)")
    gsp.add_argument("--purge", action="store_true", help="also delete the volume with flows and the guard CA")
    gsp.add_argument("--yes", action="store_true", help="confirm --purge")
    gst = gs.add_parser("status", help="active program, rate limit, last refusals")
    gst.add_argument("--json", action="store_true")
    sub.add_parser("burp", help="launch Burp with the guard as upstream proxy (127.0.0.1:8081)")
    fl = sub.add_parser("flows", help="the guard's flow log (CONTRACTS §5), read from the container")
    fl.add_argument("action", nargs="?", choices=["export"], help="export <handle> <dir>: copy flows to a Mac folder")
    fl.add_argument("args", nargs="*", metavar="ARG")
    fl.add_argument("--handle", help="program (default: the active one)")
    fl.add_argument("--tail", type=int, default=50, help="last N lines (default 50)")
    fl.add_argument("--json", action="store_true")
    sh = sub.add_parser("shell", help="a login shell in kali-bounty as hunter, every request through the guard")
    sh.add_argument("--handle", help="work dir /bounty/work/<handle> (default: the active program); "
                                     "never changes the guard's policy")
    prog = sub.add_parser("program", help="one program")
    prs = prog.add_subparsers(dest="cmd")
    u = prs.add_parser("use", help="make a program the guard's active profile "
                                   "(run via scripts/secrets.sh exec intigriti -- for INTIGRITI_USERNAME)")
    u.add_argument("handle")
    ap = prs.add_parser("apply-state", help="record where your application to a private program stands")
    ap.add_argument("handle")
    ap.add_argument("state", help="applied | accepted | rejected | withdrawn | none (clear)")
    ap.add_argument("--note", default=None)
    man = sub.add_parser("manual", help="a target you were authorised to test directly (not a platform program)")
    ms = man.add_subparsers(dest="cmd")
    ma = ms.add_parser("add", help="create a manual target profile the guard can enforce")
    ma.add_argument("name", help="a label, e.g. \"Friend's site\"; the handle becomes manual-<slug>")
    ma.add_argument("--scope", action="append", metavar="ENTRY",
                    help="in-scope: *.example.com, example.com, https://example.com/app, or a CIDR (repeatable)")
    ma.add_argument("--out", action="append", metavar="ENTRY", help="out-of-scope entry (repeatable)")
    ma.add_argument("--rate", type=float, default=5.0, metavar="RPS",
                    help="max requests/second (default 5; 0 = no automated tools)")
    ma.add_argument("--header", help="a required request header, 'Name: value'")
    ma.add_argument("--authorized-by", help="who owns the system and authorised the test")
    ma.add_argument("--auth-note", metavar="PATH", help="path to the written authorisation (kept, not copied)")
    ma.add_argument("--expires", help="date the authorisation ends, e.g. 2026-12-31")
    ma.add_argument("--force", action="store_true", help="overwrite an existing manual target")

    rc = sub.add_parser("recon", help="the look-here list (CONTRACTS §10)")
    rcs = rc.add_subparsers(dest="cmd")
    rp = rcs.add_parser("passive", help="crt.sh + DNS for a program's in-scope wildcards (no traffic to the program)")
    rp.add_argument("handle")
    rp.add_argument("--domain", help="query this base domain instead of every in-scope wildcard")
    rm = rcs.add_parser("map", help="httpx + katana on the ACTIVE program's in-scope hosts, through the guard")
    rm.add_argument("--limit", type=int, metavar="N", help="map at most N hosts")
    rl = rcs.add_parser("list", help="hosts ranked by interest")
    rl.add_argument("handle")
    rl.add_argument("--json", action="store_true")
    return p


def _scrub(text):
    tok = os.environ.get(TOKEN_ENV)
    if tok and tok in text:
        text = text.replace(tok, "***")
    return text


def _err(line):
    sys.stderr.write("bounty: " + line + "\n")
    sys.stderr.flush()  # progress lines are read live by the app (CONTRACTS §9)


def cmd_sync(args, ws):
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise BountyError("auth", "INTIGRITI_TOKEN not set (run via scripts/secrets.sh exec intigriti"
                                  " -- host/bounty …)", 2)
    client = Client(token, log=_err)
    idx = sync_mod.run(client, ws, handle=args.handle, log=_err, full=args.full)
    c = idx["counts"]
    print("synced %d programs (%d open: %d ok, %d needs-join, %d errors); %d details fetched (%s); "
          "%d new activities; %d requests; index sync/%s.json"
          % (c["programs"], c["open"], c["detail_ok"], c["needs_join"], c["detail_errors"],
             c["details_planned"], idx["mode"], c["activities_new"], idx["requests"], idx["ts"]))
    for e in idx["errors"]:
        sys.stderr.write("bounty: %s: %s: %s\n" % (e["code"], e["handle"], e["message"]))
    return 0


def status(ws):
    """CONTRACTS §9 Ranking.status: last complete (not --handle) sync and program counts."""
    store = Store(ws)
    last = None
    for ts in reversed(store.sync_timestamps()):
        idx = store.sync_index(ts) or {}
        if not idx.get("partial"):
            last = idx.get("synced_at")
            break
    progs = _intigriti(store.programs())
    open_ = [p for p in progs if p.get("status") == "open"]
    return {"last_sync_at": last, "programs": len(progs), "open": len(open_),
            "needs_join": sum(1 for p in open_ if p.get("detail") == "needs-join")}


def _intigriti(progs):
    """Ranking, counts and the Intigriti sync ignore manual targets (CONTRACTS §2, legal_basis
    "authorization"): a manual profile has no platform_id and is never sent to the API."""
    return [p for p in progs if (p.get("platform") or "intigriti") == "intigriti"]


def cmd_status(args, ws):
    st = status(ws)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print("last sync: %s" % (st["last_sync_at"] or "never (run programs sync)"))
    print("%d programs, %d open, %d need joining to see scope" % (st["programs"], st["open"], st["needs_join"]))
    return 0


def cmd_list(args, ws):
    progs = Store(ws).programs()
    if args.filter:
        q = args.filter.lower()
        progs = [p for p in progs if any(q in str(p.get(k) or "").lower() for k in
                                         ("handle", "name", "industry", "status", "confidentiality", "detail"))]
    hw = 36
    print("%-*s %-9s %-4s %-10s %7s %4s %4s  %s" % (hw, "handle", "status", "conf", "detail", "max€",
                                                    "in", "out", "name"))
    for p in sorted(progs, key=lambda p: p.get("handle") or ""):
        b = p.get("bounty") or {}
        sc = p.get("scope") or {}
        mx = b.get("max_eur")
        h = p["handle"] if len(p["handle"]) <= hw else p["handle"][:hw - 1] + "…"
        name = (p.get("name") or "")[:40]
        print("%-*s %-9s %-4s %-10s %7s %4s %4s  %s" % (
            hw, h, p.get("status") or "-", rank_mod.CONF_SHORT.get(p.get("confidentiality"), "-"),
            p.get("detail") or "-", "-" if mx is None else "%d" % round(mx),
            len(sc.get("in") or []) if p.get("scope") else "-",
            len(sc.get("out") or []) if p.get("scope") else "-", name))
    print("%d programs" % len(progs))
    return 0


def cmd_rank(args, ws):
    cfg = load_ranking(ws)
    progs = _intigriti(Store(ws).programs())
    res = rank_mod.score(progs, cfg["weights"], utcnow(), include_no_bounty=args.include_no_bounty,
                         include_join=args.include_join)
    if args.explain:
        h = args.explain.lower()
        if not h.startswith("intigriti-") and not any(r["handle"] == h for r in res):
            h = "intigriti-" + h
        text = rank_mod.render_explain(res, h, cfg["weights"])
        if text is None:
            raise BountyError("usage", "%s is not a synced open program" % args.explain, 64)
        print(text)
        return 0
    if args.json:
        data = rank_mod.to_json(res)
        apps = applications.load(ws)
        for d in data:
            d["application"] = applications.summary(ws, d["handle"], apps)
        if args.top:
            ranked = [d for d in data if not d["excluded"]][:args.top]
            data = ranked + [d for d in data if d["excluded"]]
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if not res:
        print("no open programs synced (run programs sync)")
        return 0
    print(rank_mod.render_table(res, args.top))
    return 0


def cmd_changes(args, ws):
    since = parse_time(args.since) if args.since else None
    events, msg = changes_mod.run(ws, since)
    if args.json:
        print(json.dumps([{"ts": rec["ts"], "handle": h, "kind": rec["kind"], "detail": rec["detail"]}
                          for h, rec in events], indent=2, ensure_ascii=False))
        return 0 if events else 1
    if not events:
        print(msg)
        return 1
    for h, rec in events:
        print("%-36s %-14s %s" % (h, rec["kind"], changes_mod.summary(rec["kind"], rec["detail"])))
    print("%d changes appended to history.jsonl" % len(events))
    return 0


def cmd_apply_state(args, ws):
    rec = applications.set_state(ws, args.handle.lower(), args.state.lower(), args.note)
    print("%s: %s (%s)" % (args.handle.lower(), rec["state"], rec["ts"]))
    return 0


def cmd_flows(args, ws):
    if args.action == "export":
        if len(args.args) != 2:
            raise BountyError("usage", "flows export <handle> <dir>", 64)
        return guard_mod.flows_export(args.args[0], args.args[1])
    if args.args:
        raise BountyError("usage", "unexpected arguments %s" % " ".join(args.args), 64)
    return guard_mod.flows(args.handle, args.tail, args.json)


BURP_APP = "/Applications/Burp Suite.app"


def cmd_burp(args, ws):
    """Launch Burp with every request going through the scope guard (host/burp/bounty-*.json).

    A Burp that is already open keeps its old settings: quit it first. Burp's startup screen
    still needs Next / Start Burp; the settings from these files are preselected."""
    import subprocess
    if not os.path.isdir(BURP_APP):
        raise BountyError("burp", "Burp Suite isn't installed in /Applications", 5)
    running = subprocess.run(["pgrep", "-f", "Burp Suite.app"], stdout=subprocess.DEVNULL).returncode == 0
    if running:
        _err("burp: Burp is already open; quit it first if it isn't using the guard (Settings > Network > Upstream proxy)")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = os.path.join(root, "host", "burp")
    subprocess.run(["/usr/bin/open", "-a", BURP_APP, "--args",
                    "--user-config-file=" + os.path.join(cfg, "bounty-user-options.json"),
                    "--config-file=" + os.path.join(cfg, "bounty-project-options.json")], check=True)
    print("Burp launching with upstream proxy 127.0.0.1:8081 (the guard). "
          "Check: host/bounty guard status")
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    ws = workspace_dir()
    handlers = {
        ("programs", "sync"): cmd_sync, ("programs", "status"): cmd_status, ("programs", "list"): cmd_list,
        ("programs", "rank"): cmd_rank, ("programs", "changes"): cmd_changes,
        ("guard", "start"): lambda a, w: guard_mod.guard_start(w),
        ("guard", "stop"): lambda a, w: guard_mod.guard_stop(a.purge, a.yes),
        ("flows", None): cmd_flows,
        ("guard", "status"): lambda a, w: guard_mod.guard_status(a.json),
        ("program", "apply-state"): cmd_apply_state,
        ("program", "use"): lambda a, w: guard_mod.program_use(w, a.handle),
        ("manual", "add"): lambda a, w: manual_mod.add(w, a, sys.stdout),
        ("burp", None): cmd_burp,
        ("shell", None): lambda a, w: shell_mod.shell(a.handle),
        ("recon", "passive"): lambda a, w: recon_mod.passive(w, a.handle, a.domain),
        ("recon", "map"): lambda a, w: recon_mod.map_cmd(w, a.limit),
        ("recon", "list"): lambda a, w: recon_mod.list_cmd(w, a.handle, a.json),
    }
    fn = handlers.get((args.group, getattr(args, "cmd", None)))
    if fn is None:
        parser.print_help(sys.stderr)
        return 64
    try:
        return fn(args, ws)
    except BountyError as e:
        sys.stderr.write("bounty: %s: %s\n" % (e.code, _scrub(e.message)))
        return e.exit_code
    except BrokenPipeError:  # `... --json | head`: the reader went away, not an error of ours
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("bounty: interrupted\n")
        return 130
    except Exception as e:  # never dump a traceback that could include request objects
        sys.stderr.write("bounty: internal: %s\n" % _scrub("%s: %s" % (type(e).__name__, e)))
        return 70


if __name__ == "__main__":
    sys.exit(main())
