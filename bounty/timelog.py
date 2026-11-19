"""Time tracking and €/h (CONTRACTS §14).

  time start [note] [--handle H]   open a timer (stops a running one first)
  time stop                        close it -> programs/<h>/time.jsonl {"start","end","note"}
  time status                      the running timer and today's total
  time report [--since DAYS] [--json]   hours, findings, payouts and €/h per program
"""

import datetime
import json
import os
import sys

from bounty import findings
from bounty.util import BountyError, iso, parse_time, read_json, utcnow, write_json

OPEN = "time-open.json"


def _open_path(ws):
    return os.path.join(ws, OPEN)


def _log_path(ws, handle):
    return os.path.join(ws, "programs", handle, "time.jsonl")


def _sessions(ws, handle):
    out = []
    try:
        with open(_log_path(ws, handle), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out.append((parse_time(r["start"]), parse_time(r["end"]), r.get("note") or ""))
                except (ValueError, KeyError, BountyError):
                    continue
    except FileNotFoundError:
        pass
    return out


def _fmt_h(seconds):
    m = int(round(seconds / 60.0))
    return "%dh%02d" % (m // 60, m % 60)


def _close(ws, now, out):
    cur = read_json(_open_path(ws))
    if not cur:
        return None
    start = parse_time(cur["start"])
    h = cur["handle"]
    if now > start:
        line = json.dumps({"start": cur["start"], "end": iso(now), "note": cur.get("note") or ""}, ensure_ascii=False)
        os.makedirs(os.path.dirname(_log_path(ws, h)), exist_ok=True)
        with open(_log_path(ws, h), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    os.unlink(_open_path(ws))
    out.write("stopped %s after %s\n" % (h, _fmt_h((now - start).total_seconds())))
    return h


def start(ws, note=None, handle=None, out=sys.stdout, now=None):
    h = findings.default_handle(handle)
    if not os.path.isfile(os.path.join(ws, "programs", h, "program.json")):
        raise BountyError("usage", "no program %s" % h, 64)
    now = now or utcnow()
    _close(ws, now, out)
    write_json(_open_path(ws), {"handle": h, "start": iso(now), "note": note or ""})
    out.write("timer started for %s%s\n" % (h, (": " + note) if note else ""))
    return 0


def stop(ws, out=sys.stdout, now=None):
    if not _close(ws, now or utcnow(), out):
        out.write("no timer running\n")
    return 0


def status(ws, out=sys.stdout, now=None):
    now = now or utcnow()
    cur = read_json(_open_path(ws))
    day = now.replace(hour=0, minute=0, second=0)
    total = 0.0
    progs = os.path.join(ws, "programs")
    for h in (os.listdir(progs) if os.path.isdir(progs) else []):
        for s, e, _ in _sessions(ws, h):
            total += max(0.0, (e - max(s, day)).total_seconds()) if e > day else 0.0
    if cur:
        run = (now - parse_time(cur["start"])).total_seconds()
        total += (now - max(parse_time(cur["start"]), day)).total_seconds()
        out.write("running: %s for %s%s\n" % (cur["handle"], _fmt_h(run), (" (" + cur["note"] + ")") if cur.get("note") else ""))
    else:
        out.write("no timer running\n")
    out.write("today: %s\n" % _fmt_h(total))
    return 0


def report_rows(ws, since_days=30, now=None):
    now = now or utcnow()
    cut = now - datetime.timedelta(days=since_days)
    progs = os.path.join(ws, "programs")
    handles = set(os.listdir(progs)) if os.path.isdir(progs) else set()
    cur = read_json(_open_path(ws))
    fs = findings.rows(ws)
    out = []
    for h in sorted(handles):
        secs, n = 0.0, 0
        sessions = list(_sessions(ws, h))
        if cur and cur.get("handle") == h:
            sessions.append((parse_time(cur["start"]), now, cur.get("note") or ""))
        for s, e, _ in sessions:
            part = (e - max(s, cut)).total_seconds()
            if part > 0:
                secs += part
                n += 1
        mine = [f for f in fs if f["program"] == h]
        by_status = {}
        for f in mine:
            by_status[f["status"]] = by_status.get(f["status"], 0) + 1
        paid = sum(float(f["payout_eur"]) for f in mine if isinstance(f["payout_eur"], (int, float)))
        if not secs and not mine:
            continue
        hours = round(secs / 3600.0, 2)
        name = (read_json(os.path.join(progs, h, "program.json"), {}) or {}).get("name") or h
        out.append({"handle": h, "name": name, "hours": hours, "sessions": n, "findings": by_status,
                    "payout_eur": round(paid, 2), "eur_per_hour": round(paid / hours, 2) if hours else None})
    out.sort(key=lambda r: r["hours"], reverse=True)
    out.sort(key=lambda r: -1 if r["eur_per_hour"] is None else r["eur_per_hour"], reverse=True)
    return out


def report(ws, since_days=30, as_json=False, out=sys.stdout):
    rs = report_rows(ws, since_days)
    if as_json:
        out.write(json.dumps(rs) + "\n")
        return 0
    if not rs:
        out.write("no time or findings in the last %g days: host/bounty time start\n" % since_days)
        return 0
    out.write("%-34s %7s %8s %9s  %s\n" % ("program", "hours", "€ paid", "€/h", "findings"))
    for r in rs:
        out.write("%-34s %7.2f %8.0f %9s  %s\n" % (
            r["name"][:34], r["hours"], r["payout_eur"], "-" if r["eur_per_hour"] is None else "%.0f" % r["eur_per_hour"],
            ", ".join("%s %d" % kv for kv in sorted(r["findings"].items())) or "-"))
    return 0
