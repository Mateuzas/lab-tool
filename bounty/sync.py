"""`programs sync` (BE-2, CONTRACTS §6): list + detail for open programs + new activities."""

import os
import sys

from bounty import normalise
from bounty.config import load_ranking
from bounty.store import Store
from bounty.util import (BountyError, append_jsonl, dumps, iso, iso_from_unix, read_json, ts_name,
                         utcnow, write_bytes, write_json)

PAGE = 500


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


def run(client, ws, handle=None, now=None, log=None):
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

    counts = {"programs": len(records), "open": 0, "detail_ok": 0, "needs_join": 0,
              "detail_errors": 0, "not_fetched": 0, "activities_new": len(new_acts)}
    errors, handles = [], []
    open_total = sum(1 for r in records if (normalise._value(r.get("status")) or "").lower() == "open")
    done = 0
    for r in records:
        h = assigned[r.get("id")]
        raw = store.raw_dir(h, ts)
        write_json(os.path.join(raw, "list.json"), r)
        prev = read_json(store.program_path(h)) or {}
        status = (normalise._value(r.get("status")) or "").lower()
        detail, state = None, "not-fetched"
        if status == "open":
            counts["open"] += 1
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
            if done % 20 == 0 or done == open_total:
                log("detail %d/%d" % (done, open_total))
        fs = firsts.get(r.get("id"))
        try:
            prog = normalise.program(h, r, detail, state, fx, synced_at, iso_from_unix(fs), prev)
        except Exception as e:  # unexpected shape: keep going, record it
            errors.append({"handle": h, "code": "parse", "message": "normalise: %s" % type(e).__name__})
            prog = normalise.program(h, r, None, "error", fx, synced_at, iso_from_unix(fs), prev)
            state = "error"
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
        "handles": sorted(handles),
        "counts": counts,
        "requests": client.requests,
        "errors": errors,
    }
    write_json(store.sync_index_path(ts), index)
    return index
