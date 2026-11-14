"""host/bounty entry point (CONTRACTS §6). Errors: one stderr line `bounty: <code>: <message>`."""

import argparse
import json
import os
import sys

from bounty import changes as changes_mod
from bounty import rank as rank_mod
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
    progs = store.programs()
    open_ = [p for p in progs if p.get("status") == "open"]
    return {"last_sync_at": last, "programs": len(progs), "open": len(open_),
            "needs_join": sum(1 for p in open_ if p.get("detail") == "needs-join")}


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
    progs = Store(ws).programs()
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    ws = workspace_dir()
    handlers = {"sync": cmd_sync, "status": cmd_status, "list": cmd_list, "rank": cmd_rank, "changes": cmd_changes}
    if args.group != "programs" or getattr(args, "cmd", None) not in handlers:
        parser.print_help(sys.stderr)
        return 64
    try:
        return handlers[args.cmd](args, ws)
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
