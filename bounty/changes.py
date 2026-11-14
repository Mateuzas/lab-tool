"""`programs changes` (BE-4, CONTRACTS §6): diff of program snapshots between syncs -> history.jsonl.

Each sync keeps a copy of every program.json it wrote in programs/<h>/snapshots/<sync ts>.json.
For every program in the latest sync, the latest snapshot is compared with that program's newest
snapshot at or before the baseline (the previous sync, or --since). Events already in history.jsonl
are not appended or printed again, so a second run on the same syncs reports nothing (exit 1).
"""

import json

from bounty.store import Store
from bounty.util import append_jsonl, read_json, read_jsonl, ts_name, BountyError


def _entry_key(e):
    return (e.get("type"), e.get("value"), e.get("tier"))


def _bounty_view(b):
    b = b or {}
    return {k: b.get(k) for k in ("min", "max", "currency", "min_eur", "max_eur")}


RULE_FIELDS = ("safe_harbour", "rate_limit_rps", "automation", "intigriti_me", "user_agent",
               "request_header")


def diff(prev, cur):
    """Events (kind, detail) between two program.json snapshots. prev None -> program-new."""
    if prev is None:
        b = cur.get("bounty") or {}
        return [("program-new", {"name": cur.get("name"), "status": cur.get("status"),
                                 "confidentiality": cur.get("confidentiality"),
                                 "max_eur": b.get("max_eur"), "detail": cur.get("detail")})]
    events = []
    pd, cd = prev.get("detail"), cur.get("detail")
    if pd == "needs-join" and cd == "ok":
        events.append(("access-granted", {"name": cur.get("name"), "from": pd, "to": cd}))
    elif pd == "ok" and cd == "needs-join":
        events.append(("access-lost", {"name": cur.get("name"), "from": pd, "to": cd}))
    ps, cs = prev.get("scope"), cur.get("scope")
    # Scope and rules are only compared when both snapshots have them (a 403 or an unfetched
    # detail is not a removal).
    if ps is not None and cs is not None:
        for lst in ("in", "out"):
            pk = dict((_entry_key(e), e) for e in ps.get(lst) or [])
            ck = dict((_entry_key(e), e) for e in cs.get(lst) or [])
            for k in sorted(set(ck) - set(pk), key=lambda x: tuple(str(i) for i in x)):
                e = ck[k]
                events.append(("scope-added", {"list": lst, "type": e.get("type"), "value": e.get("value"),
                                               "tier": e.get("tier"), "version": cs.get("version")}))
            for k in sorted(set(pk) - set(ck), key=lambda x: tuple(str(i) for i in x)):
                e = pk[k]
                events.append(("scope-removed", {"list": lst, "type": e.get("type"), "value": e.get("value"),
                                                 "tier": e.get("tier"), "version": cs.get("version")}))
    pb, cb = _bounty_view(prev.get("bounty")), _bounty_view(cur.get("bounty"))
    if (pb["min"], pb["max"], pb["currency"]) != (cb["min"], cb["max"], cb["currency"]):
        events.append(("bounty-changed", {"from": pb, "to": cb}))
    pr, cr = prev.get("rules"), cur.get("rules")
    if pr is not None and cr is not None:
        changed = [k for k in RULE_FIELDS if pr.get(k) != cr.get(k)]
        if pr.get("text") != cr.get("text"):
            changed.append("text")
        if pr.get("version") != cr.get("version") or changed:
            events.append(("rules-changed", {
                "from_version": pr.get("version"), "to_version": cr.get("version"),
                "fields": dict((k, {"from": pr.get(k), "to": cr.get(k)}) for k in changed if k != "text"),
                "text_changed": "text" in changed}))
    return events


def _canon(kind, detail):
    return json.dumps([kind, detail], sort_keys=True, ensure_ascii=False)


def summary(kind, d):
    if kind == "program-new":
        return "%s (%s, %s, max €%s, detail %s)" % (d.get("name"), d.get("status"), d.get("confidentiality"),
                                                   d.get("max_eur"), d.get("detail"))
    if kind in ("access-granted", "access-lost"):
        return "%s: detail %s -> %s" % (d.get("name"), d.get("from"), d.get("to"))
    if kind in ("scope-added", "scope-removed"):
        tier = (" " + d["tier"]) if d.get("tier") else ""
        return "%s %s %s%s" % (d.get("list"), d.get("type"), d.get("value"), tier)
    if kind == "bounty-changed":
        f, t = d["from"], d["to"]
        return "%s-%s %s -> %s-%s %s" % (f["min"], f["max"], f["currency"], t["min"], t["max"], t["currency"])
    if kind == "rules-changed":
        parts = ["%s %s->%s" % (k, v["from"], v["to"]) for k, v in sorted(d["fields"].items())]
        if d.get("text_changed"):
            parts.append("text")
        return "version %s -> %s%s" % (d.get("from_version"), d.get("to_version"),
                                       (": " + ", ".join(parts)) if parts else "")
    return json.dumps(d)


def run(ws, since=None):
    """Returns (events, message). events: list of (handle, record) newly appended."""
    store = Store(ws)
    syncs = store.sync_timestamps()
    if not syncs:
        return [], "no syncs yet (run programs sync)"
    latest = syncs[-1]
    if since is not None:
        cutoff = ts_name(since)
        if cutoff >= latest:
            return [], "nothing synced after %s" % cutoff
    else:
        if len(syncs) < 2:
            return [], "only one sync (%s): nothing to compare yet" % latest
        cutoff = syncs[-2]
    index = store.sync_index(latest) or {}
    synced_at = index.get("synced_at")
    out = []
    for h in index.get("handles") or []:
        cur = read_json(store.snapshot_path(h, latest))
        if cur is None:
            raise BountyError("parse", "snapshot missing: %s at %s" % (h, latest), 4)
        earlier = [t for t in store.snapshot_timestamps(h) if t <= cutoff and t < latest]
        base_ts = earlier[-1] if earlier else None
        prev = read_json(store.snapshot_path(h, base_ts)) if base_ts else None
        events = diff(prev, cur)
        if not events:
            continue
        seen = set(_canon(x.get("kind"), x.get("detail")) for x in read_jsonl(store.history_path(h)))
        new = []
        for kind, detail in events:
            detail = dict(detail)
            detail["from_sync"] = base_ts
            detail["to_sync"] = latest
            if _canon(kind, detail) in seen:
                continue
            rec = {"ts": synced_at, "kind": kind, "detail": detail}
            new.append(rec)
        append_jsonl(store.history_path(h), new)
        out.extend((h, rec) for rec in new)
    if not out:
        return [], "no new changes between %s and %s" % (cutoff, latest)
    return out, None
