"""BE-12 (CONTRACTS §9): incremental sync selection, progress lines, status/changes/rank JSON."""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bounty import cli, sync
from bounty.intigriti import Client
from bounty.util import read_json
from bounty.tests import fixtures as fx
from bounty.tests.test_sync import NOW, TOKEN, FakeAPI, make_client, world

H = datetime.timedelta(hours=1)


def details(calls):
    return sorted(c.rsplit("/", 1)[1] for c in calls
                  if c.startswith("/v1/programs/") and not c.startswith("/v1/programs/activities"))


class TestIncremental(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        recs, det, acts = world()
        self.api = FakeAPI(recs, det, acts)
        self.first = sync.run(make_client(self.api), self.ws, now=NOW, log=lambda m: None)
        self.api.calls = []

    def tearDown(self):
        shutil.rmtree(self.ws)

    def again(self, when=NOW + H, **kw):
        lines = []
        idx = sync.run(make_client(self.api), self.ws, now=when, log=lines.append, **kw)
        return idx, lines

    def test_nothing_changed_fetches_no_detail(self):
        idx, lines = self.again()
        self.assertEqual(details(self.api.calls), [])
        self.assertEqual(lines, ["progress: 0/0 up to date"])
        self.assertEqual(idx["mode"], "incremental")
        c = idx["counts"]
        self.assertEqual((c["details_planned"], c["detail_ok"], c["needs_join"], c["detail_kept"]), (0, 1, 1, 2))
        a = read_json(os.path.join(self.ws, "programs", "intigriti-acme", "program.json"))
        self.assertEqual(a["detail"], "ok")  # scope and rules carried over
        self.assertEqual(len(a["scope"]["in"]), 3)
        self.assertEqual(a["synced_at"], "2026-11-13T13:00:00Z")
        b = read_json(os.path.join(self.ws, "programs", "intigriti-beta", "program.json"))
        self.assertEqual(b["detail"], "needs-join")
        # every program still gets a snapshot, so `programs changes` sees the whole list
        self.assertEqual(sorted(idx["handles"]), ["intigriti-acme", "intigriti-beta", "intigriti-gone"])

    def test_list_record_change_triggers_detail(self):
        self.api.records[1]["maxBounty"]["value"] = 5000.0  # beta: bounty changed
        idx, lines = self.again()
        self.assertEqual(details(self.api.calls), ["p-b"])
        self.assertEqual(lines, ["progress: 1/1 intigriti-beta"])
        self.api.calls = []
        self.api.records[0]["confidentialityLevel"]["value"] = "Registered"  # acme: confidentiality
        self.again(NOW + 2 * H)
        self.assertEqual(details(self.api.calls), ["p-a"])

    def test_status_change_to_open_triggers_detail(self):
        self.api.records[2]["status"]["value"] = "Open"
        self.again()
        self.assertEqual(details(self.api.calls), ["p-c"])

    def test_new_activity_triggers_detail(self):
        self.api.activities.append(fx.activity("p-a", 1800000000, 1, "New domains version added"))
        idx, lines = self.again()
        self.assertEqual(details(self.api.calls), ["p-a"])
        self.assertEqual(idx["detail_fetched"], ["intigriti-acme"])

    def test_new_program_triggers_detail(self):
        self.api.records.append(fx.list_record(pid="p-d", handle="delta"))
        self.again()
        self.assertEqual(details(self.api.calls), ["p-d"])

    def test_stale_detail_refetched_after_seven_days(self):
        self.again(NOW + datetime.timedelta(days=6, hours=23))
        self.assertEqual(details(self.api.calls), ["p-b"])  # needs-join: rechecked after 24 h
        self.api.calls = []
        _, lines = self.again(NOW + datetime.timedelta(days=7, hours=1))
        self.assertEqual(details(self.api.calls), ["p-a"])
        self.assertEqual(lines, ["progress: 1/1 intigriti-acme"])

    def test_needs_join_rechecked_after_24h(self):
        self.again(NOW + datetime.timedelta(hours=23))
        self.assertEqual(details(self.api.calls), [])
        idx, _ = self.again(NOW + datetime.timedelta(hours=25))
        self.assertEqual(details(self.api.calls), ["p-b"])
        self.assertEqual(idx["detail_fetched"], ["intigriti-beta"])
        # the unchanged 403 is not stored again, but still counts as checked
        self.api.calls = []
        self.again(NOW + datetime.timedelta(hours=30))
        self.assertEqual(details(self.api.calls), [])

    def test_snapshot_only_on_change(self):
        idx, _ = self.again()
        self.assertEqual(idx["snapshots"], [])
        snaps = os.listdir(os.path.join(self.ws, "programs", "intigriti-acme", "snapshots"))
        raws = os.listdir(os.path.join(self.ws, "programs", "intigriti-acme", "raw"))
        self.assertEqual((len(snaps), len(raws)), (1, 1))
        self.api.records[1]["maxBounty"]["value"] = 5000.0
        idx, _ = self.again(NOW + 2 * H)
        self.assertEqual(idx["snapshots"], ["intigriti-beta"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"BOUNTY_WORKSPACE": self.ws}):
            rc = cli.main(["programs", "changes", "--json"])
        ev = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual([(e["handle"], e["kind"]) for e in ev], [("intigriti-beta", "bounty-changed")])

    def test_failed_detail_is_retried_next_time(self):
        shutil.rmtree(self.ws)
        self.ws = tempfile.mkdtemp()
        self.api.fail["/v1/programs/p-a"] = [500, 500, 500, 500]
        sync.run(make_client(self.api), self.ws, now=NOW, log=lambda m: None)
        self.api.calls = []
        idx, _ = self.again()
        self.assertEqual(details(self.api.calls), ["p-a"])
        self.assertEqual(idx["counts"]["detail_ok"], 1)

    def test_full_fetches_every_open_detail(self):
        idx, lines = self.again(full=True)
        self.assertEqual(details(self.api.calls), ["p-a", "p-b"])
        self.assertEqual(idx["mode"], "full")
        self.assertEqual(len(lines), 2)

    def test_handle_always_fetches(self):
        self.again(handle="acme")
        self.assertEqual(details(self.api.calls), ["p-a"])


class TestCliJson(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        recs, det, acts = world()
        self.api = FakeAPI(recs, det, acts)
        self.env = {"INTIGRITI_TOKEN": TOKEN, "BOUNTY_WORKSPACE": self.ws}

    def tearDown(self):
        shutil.rmtree(self.ws)

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        api = self.api
        real = Client.__init__

        def init(c, token, transport=None, sleep=None, clock=None, log=None):
            real(c, token, transport=api, sleep=lambda s: None, log=log)
        with mock.patch.dict(os.environ, self.env, clear=False), mock.patch.object(Client, "__init__", init), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_status_json(self):
        rc, out, _ = self.cli(["programs", "status", "--json"])
        self.assertEqual((rc, json.loads(out)),
                         (0, {"last_sync_at": None, "programs": 0, "open": 0, "needs_join": 0}))
        with mock.patch.object(sync, "utcnow", return_value=NOW):
            self.cli(["programs", "sync"])
        rc, out, _ = self.cli(["programs", "status", "--json"])
        self.assertEqual(json.loads(out), {"last_sync_at": "2026-11-13T12:00:00Z", "programs": 3, "open": 2,
                                           "needs_join": 1})

    def test_progress_lines_on_stderr(self):
        rc, out, err = self.cli(["programs", "sync"])
        self.assertEqual(rc, 0)
        prog = [line for line in err.splitlines() if line.startswith("bounty: progress: ")]
        self.assertEqual(prog, ["bounty: progress: 1/2 intigriti-acme", "bounty: progress: 2/2 intigriti-beta"])
        self.assertNotIn("progress", out)

    def test_rank_json_has_name_and_url(self):
        self.cli(["programs", "sync"])
        rc, out, _ = self.cli(["programs", "rank", "--json", "--include-join"])
        data = json.loads(out)
        a = [d for d in data if d["handle"] == "intigriti-acme"][0]
        self.assertEqual(a["name"], "Acme")
        self.assertTrue(a["url"].startswith("https://app.intigriti.com/"))

    def test_changes_json(self):
        with mock.patch.object(sync, "utcnow", return_value=NOW):
            self.cli(["programs", "sync"])
        rc, out, _ = self.cli(["programs", "changes", "--json"])
        self.assertEqual((rc, json.loads(out)), (1, []))  # only one sync: nothing to compare
        self.api.records[0]["maxBounty"]["value"] = 20000.0
        with mock.patch.object(sync, "utcnow", return_value=NOW + H):
            self.cli(["programs", "sync"])
        rc, out, _ = self.cli(["programs", "changes", "--json"])
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual([(d["handle"], d["kind"]) for d in data], [("intigriti-acme", "bounty-changed")])
        self.assertEqual(set(data[0]), {"ts", "handle", "kind", "detail"})
        self.assertEqual(data[0]["ts"], "2026-11-13T13:00:00Z")
        rc, out, _ = self.cli(["programs", "changes", "--json"])
        self.assertEqual((rc, json.loads(out)), (1, []))  # already reported


if __name__ == "__main__":
    unittest.main()
