"""host/bounty guard / program use (CONTRACTS §3.3, §6): the parts that need no Docker."""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bounty import guard
from bounty.util import BountyError


def write_profile(ws, handle, **rules):
    r = {"rate_limit_rps": 5, "user_agent": None, "request_header": "X-Intigriti-Username: {Username}"}
    r.update(rules)
    p = {"schema": 1, "handle": handle, "detail": "ok",
         "scope": {"in": [{"type": "wildcard", "value": "*.acme.test"}], "out": []}, "rules": r}
    d = os.path.join(ws, "programs", handle)
    os.makedirs(d)
    with open(os.path.join(d, "program.json"), "w") as f:
        json.dump(p, f)


class ProgramUse(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws)

    def code(self, handle, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch.object(guard, "_docker", side_effect=AssertionError("no docker in this test")):
            try:
                guard.program_use(self.ws, handle, out=io.StringIO())
            except BountyError as e:
                return e.exit_code
        return 0

    def test_unknown_handle_is_usage(self):
        self.assertEqual(self.code("intigriti-nope"), 64)
        self.assertEqual(self.code("../etc"), 64)

    def test_unknown_placeholder_exits_4_before_docker(self):
        write_profile(self.ws, "intigriti-acme", request_header="X-Intigriti-Username: {Handle}")
        self.assertEqual(self.code("intigriti-acme"), 4)

    def test_unparseable_profile_exits_4(self):
        os.makedirs(os.path.join(self.ws, "programs", "intigriti-bad"))
        with open(os.path.join(self.ws, "programs", "intigriti-bad", "program.json"), "w") as f:
            f.write("{")
        self.assertEqual(self.code("intigriti-bad"), 4)

    def test_container_not_running_exits_5(self):
        write_profile(self.ws, "intigriti-acme")
        with mock.patch.object(guard, "running", return_value=False):
            self.assertEqual(self.code("acme", {"INTIGRITI_USERNAME": "tester"}), 5)


class RunArgs(unittest.TestCase):
    def test_contract_flags(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            args = guard.run_args(guard._cfg(), "/ws")
        self.assertEqual(args, ["run", "-d", "--name", "kali-bounty", "--init", "--restart", "unless-stopped",
                                "--cap-add=NET_ADMIN", "-p", "127.0.0.1:8081:8080", "-v", "/ws:/bounty",
                                "kali-lab:dev", "bounty-supervisor"])
        self.assertNotIn("--privileged", args)


if __name__ == "__main__":
    unittest.main()
