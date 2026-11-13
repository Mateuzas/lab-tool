"""Workspace layout (CONTRACTS §1) for programs, activities, syncs and snapshots."""

import glob
import os

from bounty.util import read_json, read_jsonl


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
