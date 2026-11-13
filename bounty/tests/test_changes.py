import copy
import shutil
import tempfile
import unittest

from bounty import changes, normalise
from bounty.store import Store
from bounty.util import read_jsonl, write_json, parse_time
from bounty.tests import fixtures as fx


def snap(**kw):
    rec = fx.list_record(**{k: kw.pop(k) for k in list(kw) if k in ("max_b", "min_b", "currency", "pid", "handle")})
    return normalise.program("intigriti-" + rec["handle"], rec, fx.detail(rec, **kw), "ok", fx.FX, "t", None)


class TestDiff(unittest.TestCase):
    def test_kinds(self):
        a = snap()
        b = copy.deepcopy(a)
        b["scope"]["in"].append({"type": "wildcard", "value": "*.new.example", "tier": "tier-1", "description": ""})
        b["scope"]["in"] = [e for e in b["scope"]["in"] if e["value"] != "www.acme.example"]
        b["bounty"]["max"] = 20000
        b["rules"]["version"] = "roe-v2"
        b["rules"]["rate_limit_rps"] = 2
        ev = changes.diff(a, b)
        kinds = [k for k, _ in ev]
        self.assertEqual(kinds, ["scope-added", "scope-removed", "bounty-changed", "rules-changed"])
        self.assertEqual(ev[0][1]["value"], "*.new.example")
        self.assertEqual(ev[1][1]["value"], "www.acme.example")
        self.assertEqual(ev[3][1]["fields"], {"rate_limit_rps": {"from": 5, "to": 2}})
        self.assertEqual(changes.diff(a, copy.deepcopy(a)), [])
        self.assertEqual(changes.diff(None, a)[0][0], "program-new")

    def test_needs_join_is_not_removal(self):
        a = snap()
        b = copy.deepcopy(a)
        b["detail"], b["scope"], b["rules"] = "needs-join", None, None
        self.assertEqual(changes.diff(a, b), [])


class TestRun(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.store = Store(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws)

    def write_sync(self, ts, progs):
        for p in progs:
            write_json(self.store.snapshot_path(p["handle"], ts), p)
            write_json(self.store.program_path(p["handle"]), p)
        write_json(self.store.sync_index_path(ts), {"ts": ts, "synced_at": ts, "handles": [p["handle"] for p in progs]})

    def test_two_syncs(self):
        a1 = snap()
        self.write_sync("20261101T000000Z", [a1])
        ev, msg = changes.run(self.ws)
        self.assertEqual(ev, [])
        self.assertIn("only one sync", msg)
        a2 = copy.deepcopy(a1)
        a2["scope"]["in"].append({"type": "domain", "value": "new.acme.example", "tier": "tier-2", "description": ""})
        newp = snap(pid="p-new", handle="newco")
        self.write_sync("20261102T000000Z", [a2, newp])
        ev, _ = changes.run(self.ws)
        got = sorted((h, r["kind"]) for h, r in ev)
        self.assertEqual(got, [("intigriti-acme", "scope-added"), ("intigriti-newco", "program-new")])
        hist = read_jsonl(self.store.history_path("intigriti-acme"))
        self.assertEqual(len(hist), 1)
        self.assertEqual(set(hist[0]), {"ts", "kind", "detail"})
        self.assertEqual(hist[0]["detail"]["from_sync"], "20261101T000000Z")
        # second run: already recorded -> nothing new
        ev, msg = changes.run(self.ws)
        self.assertEqual(ev, [])
        self.assertEqual(len(read_jsonl(self.store.history_path("intigriti-acme"))), 1)
        # --since before the first sync: everything is new relative to then
        ev, _ = changes.run(self.ws, since=parse_time("2026-10-01T00:00:00Z"))
        self.assertEqual(sorted(r["kind"] for _, r in ev), ["program-new"])  # newco already recorded
        self.assertEqual(ev[0][0], "intigriti-acme")


if __name__ == "__main__":
    unittest.main()
