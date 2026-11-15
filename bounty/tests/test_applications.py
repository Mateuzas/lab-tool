"""BE-21 (CONTRACTS §11.1) application state, §10.5 program-closed / program-reopened."""

import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bounty import applications, changes, cli
from bounty.store import Store
from bounty.util import read_json, write_json
from bounty.tests.test_changes import snap


class TestApplyState(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.store = Store(self.ws)
        p = snap()
        write_json(self.store.program_path(p["handle"]), p)
        self.h = p["handle"]

    def tearDown(self):
        shutil.rmtree(self.ws)

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"BOUNTY_WORKSPACE": self.ws}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            return cli.main(argv), out.getvalue(), err.getvalue()

    def test_apply_state_writes_and_validates(self):
        rc, _, _ = self.cli(["program", "apply-state", self.h, "applied", "--note", "sent CV"])
        self.assertEqual(rc, 0)
        data = read_json(applications.path(self.ws))
        self.assertEqual(data[self.h]["state"], "applied")
        self.assertEqual(data[self.h]["note"], "sent CV")
        self.assertEqual(self.cli(["program", "apply-state", self.h, "none"])[0], 0)
        self.assertNotIn(self.h, read_json(applications.path(self.ws)))
        self.assertEqual(self.cli(["program", "apply-state", self.h, "none"])[0], 0)  # nothing to clear
        self.assertEqual(self.cli(["program", "apply-state", self.h, "maybe"])[0], 64)
        self.assertEqual(self.cli(["program", "apply-state", "intigriti-nope", "applied"])[0], 64)

    def test_rank_json_has_application(self):
        self.cli(["program", "apply-state", self.h, "rejected"])
        rc, out, _ = self.cli(["programs", "rank", "--json", "--include-join"])
        items = {d["handle"]: d for d in json.loads(out)}
        self.assertEqual(items[self.h]["application"]["state"], "rejected")
        self.assertEqual(set(items[self.h]["application"]), {"state", "ts"})

    def write_sync(self, ts, progs):
        for p in progs:
            write_json(self.store.snapshot_path(p["handle"], ts), p)
            write_json(self.store.program_path(p["handle"]), p)
        write_json(self.store.sync_index_path(ts), {"ts": ts, "synced_at": ts,
                                                    "handles": [p["handle"] for p in progs]})

    def test_access_granted_auto_accepts(self):
        a = snap()
        j = copy.deepcopy(a)
        j["detail"], j["scope"], j["rules"] = "needs-join", None, None
        self.write_sync("20261101T000000Z", [j])
        applications.set_state(self.ws, self.h, "applied", "hi")
        self.write_sync("20261102T000000Z", [a])
        ev, _ = changes.run(self.ws)
        self.assertEqual([r["kind"] for _, r in ev], ["access-granted"])
        rec = applications.load(self.ws)[self.h]
        self.assertEqual((rec["state"], rec["note"]), ("accepted", "hi"))

    def test_auto_accept_only_from_applied(self):
        applications.set_state(self.ws, self.h, "rejected")
        self.assertFalse(applications.auto_accept(self.ws, self.h))
        self.assertFalse(applications.auto_accept(self.ws, "intigriti-other"))


class TestClosed(unittest.TestCase):
    def test_closed_and_reopened(self):
        a = snap()
        s = copy.deepcopy(a)
        s["status"] = "suspended"
        ev = changes.diff(a, s)
        self.assertEqual([k for k, _ in ev], ["program-closed"])
        self.assertEqual(ev[0][1], {"name": a["name"], "from": "open", "to": "suspended"})
        c = copy.deepcopy(a)
        c["status"] = "closed"
        self.assertEqual([k for k, _ in changes.diff(a, c)], ["program-closed"])
        self.assertEqual([k for k, _ in changes.diff(s, a)], ["program-reopened"])
        self.assertEqual(changes.diff(s, c), [])
        self.assertIn("open -> suspended", changes.summary("program-closed", ev[0][1]))


if __name__ == "__main__":
    unittest.main()
