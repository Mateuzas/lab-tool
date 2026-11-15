"""Application state for private programs (CONTRACTS §11.1, BE-21): workspace/bounty/applications.json.

{"<handle>": {"state": "applied|accepted|rejected|withdrawn", "ts": "<ISO>", "note": "<text>"}}
Written only by the CLI (`program apply-state`) and by `programs changes` (access-granted
turns `applied` into `accepted`).
"""

import os

from bounty.store import Store
from bounty.util import BountyError, iso, read_json, utcnow, write_json

STATES = ("applied", "accepted", "rejected", "withdrawn")


def path(ws):
    return os.path.join(ws, "applications.json")


def load(ws):
    data = read_json(path(ws)) or {}
    return data if isinstance(data, dict) else {}


def set_state(ws, handle, state, note=None, now=None):
    """`none` removes the entry (the app's `a` key cycles back to no state)."""
    if state == "none":
        if not os.path.isfile(Store(ws).program_path(handle)):
            raise BountyError("usage", "no program.json for %s" % handle, 64)
        data = load(ws)
        data.pop(handle, None)
        write_json(path(ws), data)
        return {"state": "none", "ts": iso(now or utcnow()), "note": ""}
    if state not in STATES:
        raise BountyError("usage", "unknown state %r (one of %s)" % (state, ", ".join(STATES)), 64)
    if not os.path.isfile(Store(ws).program_path(handle)):
        raise BountyError("usage", "no program.json for %s" % handle, 64)
    data = load(ws)
    rec = {"state": state, "ts": iso(now or utcnow()), "note": note or ""}
    if note is None and handle in data:
        rec["note"] = data[handle].get("note") or ""
    data[handle] = rec
    write_json(path(ws), data)
    return rec


def auto_accept(ws, handle, now=None):
    """access-granted: an `applied` program becomes `accepted`. Returns True if it changed."""
    data = load(ws)
    rec = data.get(handle)
    if not isinstance(rec, dict) or rec.get("state") != "applied":
        return False
    data[handle] = {"state": "accepted", "ts": iso(now or utcnow()), "note": rec.get("note") or ""}
    write_json(path(ws), data)
    return True


def summary(ws, handle, data=None):
    rec = (load(ws) if data is None else data).get(handle)
    if not isinstance(rec, dict) or rec.get("state") not in STATES:
        return None
    return {"state": rec["state"], "ts": rec.get("ts")}
