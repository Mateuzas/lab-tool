"""Workspace layout (CONTRACTS §1) for programs, activities, syncs and snapshots."""

import datetime
import glob
import os

from bounty.util import read_json, read_jsonl, write_json


class Store(object):
    def __init__(self, ws):
        self.ws = ws

    def program_dir(self, handle):
        return os.path.join(self.ws, "programs", handle)

    def program_path(self, handle):
        return os.path.join(self.program_dir(handle), "program.json")

    def snapshot_path(self, handle, ts):
        return os.path.join(self.program_dir(handle), "snapshots", ts + ".json")

    def raw_dir(self, handle, ts):
        return os.path.join(self.program_dir(handle), "raw", ts)

    def history_path(self, handle):
        return os.path.join(self.program_dir(handle), "history.jsonl")

    def activities_path(self):
        return os.path.join(self.ws, "activities.jsonl")

    def sync_index_path(self, ts):
        return os.path.join(self.ws, "sync", ts + ".json")

    def handles(self):
        out = []
        for p in glob.glob(os.path.join(self.ws, "programs", "*", "program.json")):
            out.append(os.path.basename(os.path.dirname(p)))
        return sorted(out)

    def programs(self):
        res = []
        for h in self.handles():
            p = read_json(self.program_path(h))
            if isinstance(p, dict):
                res.append(p)
        return res

    def sync_timestamps(self):
        names = glob.glob(os.path.join(self.ws, "sync", "*.json"))
        return sorted(os.path.basename(n)[:-5] for n in names)

    def sync_index(self, ts):
        return read_json(self.sync_index_path(ts))

    def snapshot_timestamps(self, handle):
        names = glob.glob(os.path.join(self.program_dir(handle), "snapshots", "*.json"))
        return sorted(os.path.basename(n)[:-5] for n in names)

    def activities(self):
        return read_jsonl(self.activities_path())

    def detail_check_path(self, handle):
        return os.path.join(self.program_dir(handle), "detail-checked.json")

    def mark_detail_checked(self, handle, ts, status):
        """Record a detail response even when its raw copy isn't kept (CONTRACTS §11.2)."""
        write_json(self.detail_check_path(handle), {"ts": ts, "status": status})

    def latest_raw_file(self, handle, name):
        """Bytes of the newest raw/<ts>/<name>, or None."""
        for d in sorted(glob.glob(os.path.join(self.program_dir(handle), "raw", "*")), reverse=True):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return f.read()
        return None

    def last_detail_time(self, handle):
        """When the newest stored detail response (detail.json or detail-<status>.txt) was fetched:
        the UTC datetime of its raw/<ts>/ folder, or None if there is none."""
        best = None
        for d in glob.glob(os.path.join(self.program_dir(handle), "raw", "*")):
            ts = os.path.basename(d)
            if best is not None and ts <= best:
                continue
            try:
                names = os.listdir(d)
            except OSError:
                continue
            if any(n == "detail.json" or (n.startswith("detail-") and n.endswith(".txt")) for n in names):
                best = ts
        chk = (read_json(self.detail_check_path(handle)) or {}).get("ts")
        if isinstance(chk, str) and (best is None or chk > best):
            best = chk
        if best is None:
            return None
        try:
            return datetime.datetime.strptime(best, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
