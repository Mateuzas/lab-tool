"""CONTRACTS §3.5: program browse (approve browse-only hosts)."""

import io
import json
import os
import shutil
import tempfile
import unittest

from bounty import browse
from bounty.util import BountyError


class Browse(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws)
        d = os.path.join(self.ws, "programs", "test-acme")
        os.makedirs(d)
        with open(os.path.join(d, "program.json"), "w") as f:
            json.dump({"schema": 1, "handle": "test-acme", "detail": "ok", "status": "open",
                       "scope": {"in": [{"type": "wildcard", "value": "*.acme.test"}],
                                 "out": [{"type": "domain", "value": "community.acme.test"}]},
                       "rules": {"rate_limit_rps": 2, "automation": "allowed"}}, f)

    def test_allow_and_remove(self):
        browse.allow(self.ws, ["CDN.AcmeStatic.test."], handle="test-acme", out=io.StringIO())
        browse.allow(self.ws, ["login.idp.test"], any_method=True, handle="test-acme", out=io.StringIO())
        cur = browse.load(self.ws, "test-acme")
        self.assertEqual({k: v["methods"] for k, v in cur.items()}, {"cdn.acmestatic.test": "read", "login.idp.test": "any"})
        browse.remove(self.ws, ["cdn.acmestatic.test"], handle="test-acme", out=io.StringIO())
        self.assertEqual(list(browse.load(self.ws, "test-acme")), ["login.idp.test"])

    def test_refusals(self):
        for host in ("community.acme.test", "app.acme.test", "10.0.0.1", "not a host", ""):
            with self.assertRaises(BountyError, msg=host):
                browse.allow(self.ws, [host], handle="test-acme", out=io.StringIO())
        self.assertEqual(browse.load(self.ws, "test-acme"), {})

    def test_candidates_from_your_refused_traffic_only(self):
        flows = json.dumps([
            {"decision": "refused", "reason": "not-in-scope", "actor": "human", "host": "cdn.x.test", "method": "GET", "ts": "2026-11-19T10:00:00Z"},
            {"decision": "refused", "reason": "not-in-scope", "actor": "human", "host": "cdn.x.test", "method": "GET", "ts": "2026-11-19T11:00:00Z"},
            {"decision": "refused", "reason": "not-in-scope", "actor": "tool:katana", "host": "tools.x.test", "method": "GET", "ts": "x"},
            {"decision": "refused", "reason": "out-of-scope", "actor": "human", "host": "community.acme.test", "method": "GET", "ts": "x"},
            {"decision": "refused", "reason": "not-in-scope", "actor": "human", "host": "burpsuite", "method": "GET", "ts": "x"},
            {"decision": "allowed", "reason": None, "actor": "human", "host": "app.acme.test", "method": "GET", "ts": "x"},
        ])
        c = browse.candidates("test-acme", flows)
        self.assertEqual(list(c), ["cdn.x.test"])
        self.assertEqual((c["cdn.x.test"]["count"], c["cdn.x.test"]["last"]), (2, "2026-11-19T11:00:00Z"))
        out = io.StringIO()
        browse.list_cmd(self.ws, "test-acme", True, out=out, flows_json=flows)
        self.assertEqual(sorted(json.loads(out.getvalue())["candidates"]), ["cdn.x.test"])


if __name__ == "__main__":
    unittest.main()
