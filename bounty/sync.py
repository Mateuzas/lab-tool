"""`programs sync` (BE-2, BE-12, CONTRACTS §6, §9): list + new activities + detail for open programs.

Incremental by default: the detail is fetched only for open programs that need it (see
`detail_reason`); `full=True` fetches every open program's detail. Progress goes to `log` as
`progress: <done>/<total> <handle>`, one line per detail request (`progress: 0/0 up to date` when
none is needed).
"""

import datetime
import os
import sys

from bounty import normalise
from bounty.config import load_ranking
from bounty.store import Store
from bounty.util import (BountyError, append_jsonl, dumps, iso, iso_from_unix, read_json, ts_name,
                         utcnow, write_bytes, write_json)

PAGE = 500
DETAIL_MAX_AGE = datetime.timedelta(days=7)
DETAIL_ACTIVITY_TYPES = ("domains", "rules", "status")  # activity type values that mean "re-fetch"


def activity_key(rec):
    t = rec.get("type")
    tkey = t.get("id") if isinstance(t, dict) and t.get("id") is not None else normalise._value(t)
    return "%s|%s|%s" % (rec.get("programId"), tkey, rec.get("createdAt"))


def _records(body, what):
    if not isinstance(body, dict) or not isinstance(body.get("records"), list):
        raise BountyError("parse", "%s: expected {maxCount, records[]}" % what, 4)
    return body["records"], body.get("maxCount")


def fetch_programs(client):
    out, offset = [], 0
    while True:
        try:
            _, body = client.programs_page(PAGE, offset)
        except BountyError as e:
            if e.exit_code == 3 and "HTTP 403" in e.message:
                raise BountyError("auth", "token not allowed to list programs (403)", 2)
            raise
        recs, total = _records(body, "/v1/programs")
        out.extend(recs)
        offset += len(recs)
        if not recs or total is None or offset >= int(total):
            return out


def fetch_new_activities(client, known):
    """Pages newest-first until a page contains an event we already have (or the end)."""
    new, seen, offset = [], set(), 0
    while True:
        _, body = client.activities_page(PAGE, offset)
        recs, total = _records(body, "/v1/programs/activities")
        hit_known = False
        for r in recs:
            k = activity_key(r)
            if k in known:
                hit_known = True
            elif k not in seen:
                seen.add(k)
                new.append(r)
        offset += len(recs)
        if hit_known or not recs or total is None or offset >= int(total):
            break
    new.sort(key=lambda r: (r.get("createdAt") or 0))
    return new


def assign_handles(records, existing):
    """platform_id -> handle. Existing directories keep their handle; a new program whose
    'intigriti-<handle>' is taken (or shared with another program in this list) gets
    'intigriti-<company>-<handle>', then a short id suffix if that is taken too."""
    by_id = dict(existing)  # platform_id -> handle
    taken = set(by_id.values())
    base = {}
    counts = {}
    for r in records:
        b = "intigriti-" + normalise.slug(r.get("handle") or r.get("name") or "")
        base[r.get("id")] = b
        counts[b] = counts.get(b, 0) + 1
    for r in sorted(records, key=lambda r: str(r.get("id"))):
        pid = r.get("id")
        if pid in by_id:
            continue
        b = base[pid]
        h = b
        if h in taken or counts[b] > 1:
            company = normalise.company_from_weblink((r.get("webLinks") or {}).get("detail"))
            if company:
                h = "intigriti-%s-%s" % (normalise.slug(company), b[len("intigriti-"):])
        if h in taken:
            h = "%s-%s" % (h, normalise.slug(str(pid))[:8])
        by_id[pid] = h
        taken.add(h)
    return by_id


def first_seen(activities):
    first = {}
    for a in activities:
        pid, t = a.get("programId"), a.get("createdAt")
        if pid is None or t is None:
            continue
        if pid not in first or t < first[pid]:
            first[pid] = t
    return first


def _match(record, handle_arg, assigned):
    h = handle_arg.lower()
    return h in (assigned.get(record.get("id")), (record.get("handle") or "").lower(),
                 "intigriti-" + (record.get("handle") or "").lower(), str(record.get("id")))


def _list_view(record, fx):
    """The list-record fields whose change triggers a detail re-fetch (as program.json stores them)."""
    b = normalise.bounty(record, fx)
    conf = (normalise._value(record.get("confidentialityLevel")) or "").strip().lower()
    return {"status": (normalise._value(record.get("status")) or "").strip().lower() or None,
            "confidentiality": normalise.CONF_MAP.get(conf, conf or None),
            "bounty": (b["min"], b["max"], b["currency"])}


def _activity_triggers(act):
    v = (normalise._value(act.get("type")) or "").lower()
    return any(w in v for w in DETAIL_ACTIVITY_TYPES)


def detail_reason(record, prev, fx, last_detail, now, new_act_pids):
    """Why this open program needs its detail fetched in an incremental sync, or None (CONTRACTS §9).

    prev: the program.json from the last sync ({} if none); last_detail: datetime of the newest
    stored detail response (200 or 403) or None; new_act_pids: program ids with a new domains,
    rules or status activity since the last sync.
    """
    if not prev:
        return "new"
    cur = _list_view(record, fx)
    pb = prev.get("bounty") or {}
    if (cur["status"] != prev.get("status") or cur["confidentiality"] != prev.get("confidentiality")
            or cur["bounty"] != (pb.get("min"), pb.get("max"), pb.get("currency"))):
        return "list-changed"
    if record.get("id") in new_act_pids:
        return "activity"
    if prev.get("detail") not in ("ok", "needs-join") or last_detail is None:
        return "no-detail"
    if now - last_detail > DETAIL_MAX_AGE:
        return "stale"
    return None


def run(client, ws, handle=None, now=None, log=None, full=False):
    log = log or (lambda m: sys.stderr.write(m + "\n"))
    now = now or utcnow()
    ts = ts_name(now)
    synced_at = iso(now)
    store = Store(ws)
    cfg = load_ranking(ws)
    fx = cfg["fx"]

    records = fetch_programs(client)
    existing = {}
    for p in store.programs():
        if p.get("platform_id"):
            existing[p["platform_id"]] = p["handle"]
    assigned = assign_handles(records, existing)

    if handle:
        records = [r for r in records if _match(r, handle, assigned)]
        if not records:
            raise BountyError("usage", "no program %r in the Intigriti list" % handle, 64)

    known_acts = store.activities()
    known = set(activity_key(a) for a in known_acts)
    new_acts = fetch_new_activities(client, known)
    append_jsonl(store.activities_path(), new_acts)
    firsts = first_seen(known_acts + new_acts)
    act_pids = set(a.get("programId") for a in new_acts if _activity_triggers(a))

    # Plan: which open programs get a detail request this run.
    prevs, plan = {}, {}
    for r in records:
        h = assigned[r.get("id")]
        prevs[h] = read_json(store.program_path(h)) or {}
        if (normalise._value(r.get("status")) or "").lower() != "open":
            continue
        if full or handle:
            plan[h] = "full"
            continue
        why = detail_reason(r, prevs[h], fx, store.last_detail_time(h), now, act_pids)
        if why:
            plan[h] = why
    total = len(plan)
    if total == 0:
        log("progress: 0/0 up to date")

    counts = {"programs": len(records), "open": 0, "detail_ok": 0, "needs_join": 0,
              "detail_errors": 0, "not_fetched": 0, "detail_kept": 0, "activities_new": len(new_acts)}
    counts["details_planned"] = total
    errors, handles = [], []
    done = 0
    for r in records:
        h = assigned[r.get("id")]
        raw = store.raw_dir(h, ts)
        write_json(os.path.join(raw, "list.json"), r)
        prev = prevs[h]
        status = (normalise._value(r.get("status")) or "").lower()
        detail, state = None, "not-fetched"
        if status == "open":
            counts["open"] += 1
        if h in plan:
            done += 1
            try:
                resp = client.program_detail(r.get("id"))
            except BountyError as e:
                if e.exit_code != 3:
                    raise
                resp = None
                state = "error"
                errors.append({"handle": h, "code": e.code, "message": e.message})
            if resp is None:
                pass
            elif resp.status == 200:
                write_bytes(os.path.join(raw, "detail.json"), resp.body)
                try:
                    detail = resp.json()
                    if not isinstance(detail, dict):
                        raise BountyError("parse", "detail is not an object", 4)
                    state = "ok"
                except BountyError as e:
                    state = "error"
                    errors.append({"handle": h, "code": "parse", "message": e.message})
            else:
                write_bytes(os.path.join(raw, "detail-%d.txt" % resp.status), resp.body)
                if resp.status == 403:
                    state = "needs-join"
                else:
                    state = "error"
                    errors.append({"handle": h, "code": "api", "message": "detail HTTP %d" % resp.status})
            log("progress: %d/%d %s" % (done, total, h))
        fs = firsts.get(r.get("id"))
        try:
            prog = normalise.program(h, r, detail, state, fx, synced_at, iso_from_unix(fs), prev)
        except Exception as e:  # unexpected shape: keep going, record it
            errors.append({"handle": h, "code": "parse", "message": "normalise: %s" % type(e).__name__})
            prog = normalise.program(h, r, None, "error", fx, synced_at, iso_from_unix(fs), prev)
            state = "error"
        if state == "not-fetched" and status == "open":
            counts["detail_kept"] += 1
            state = prog["detail"] if prog["detail"] in ("ok", "needs-join") else state
        key = {"ok": "detail_ok", "needs-join": "needs_join", "error": "detail_errors",
               "not-fetched": "not_fetched"}[state]
        counts[key] += 1
        data = dumps(prog).encode("utf-8")
        write_bytes(store.program_path(h), data)
        write_bytes(store.snapshot_path(h, ts), data)
        handles.append(h)

    index = {
        "ts": ts,
        "synced_at": synced_at,
        "finished_at": iso(utcnow()),
        "partial": bool(handle),
        "mode": "full" if (full or handle) else "incremental",
        "detail_fetched": sorted(plan),
        "handles": sorted(handles),
        "counts": counts,
        "requests": client.requests,
        "errors": errors,
    }
    write_json(store.sync_index_path(ts), index)
    return index
