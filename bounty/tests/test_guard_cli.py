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


class Purge(unittest.TestCase):
    def test_purge_needs_yes(self):
        with mock.patch.object(guard, "_docker", side_effect=AssertionError("must not run docker")):
            with self.assertRaises(BountyError) as cm:
                guard.guard_stop(purge=True, yes=False, out=io.StringIO())
        self.assertEqual(cm.exception.exit_code, 64)


class RunArgs(unittest.TestCase):
    def _args(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            return guard.run_args(guard._cfg(), "/ws")

    def test_contract_flags(self):
        args = self._args()
        self.assertEqual(args[:9], ["run", "-d", "--name", "kali-bounty", "--init", "--restart", "unless-stopped",
                                    "--label", "kali-lab.guard-spec=" + guard.SPEC])
        self.assertEqual(args[-2:], ["kali-lab:dev", "bounty-supervisor"])
        for pair in (["-p", "127.0.0.1:8081:8080"], ["-v", "/ws:/bounty:ro"],
                     ["-v", "/ws/work:/bounty/work:rw"], ["-v", "kali-bounty-guard:/var/lib/bounty-guard"],
                     ["-v", "kali-bounty-home:/home/hunter"], ["--security-opt", "no-new-privileges:true"]):
            self.assertTrue(any(args[j:j + 2] == pair for j in range(len(args))), pair)
        self.assertEqual(sum(1 for a in args if a == "-p"), 1)   # only the human port, never 8082
        self.assertEqual(sum(1 for a in args if a == "-v"), 4)   # nothing else of the Mac is mounted
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--add-host", args)                     # test-only, off by default

    def test_add_hosts_for_tests(self):
        with mock.patch.dict(os.environ, {"BOUNTY_GUARD_ADD_HOSTS": " a.test:203.0.113.10  b.test:10.0.0.1 "}, clear=True):
            args = guard.run_args(guard._cfg(), "/ws")
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--add-host"],
                         ["a.test:203.0.113.10", "b.test:10.0.0.1"])

    def test_hardening(self):
        args = self._args()
        self.assertIn("--read-only", args)
        # every capability is dropped first; only the supervisor's own needs come back
        self.assertLess(args.index("--cap-drop=ALL"), min(i for i, a in enumerate(args) if a.startswith("--cap-add")))
        added = {a.split("=", 1)[1] for a in args if a.startswith("--cap-add=")}
        self.assertEqual(added, set(guard.CAPS))
        self.assertTrue({"NET_ADMIN", "SETUID", "SETGID"} <= added)
        self.assertFalse({"SYS_ADMIN", "NET_RAW", "SYS_PTRACE", "MKNOD"} & added)
        tmpfs = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
        self.assertEqual([t.split(":")[0] for t in tmpfs], [p for p, _ in guard.TMPFS])
        self.assertIn("/tmp:rw,nosuid,nodev,mode=1777", tmpfs)   # hunter's tools need a scratch dir
        for t in tmpfs:
            self.assertIn("nosuid", t)
            self.assertIn("nodev", t)


class Recreate(unittest.TestCase):
    def _start(self, spec):
        calls = []

        def fake(args, **_kw):
            calls.append(args)
            if args[0] == "inspect" and "-f" in args and "guard-spec" in args[2]:
                return 0, spec + "\n", ""
            if args[0] == "inspect" and args[-1] == "kali-lab":
                return 0, "false\n", ""
            if args[0] == "inspect":
                return (1 if ["rm", "-f", "kali-bounty"] in calls else 0), "", ""
            if args[0] == "exec":
                return 0, '{"guard": "up", "status": "no-profile"}', ""
            return 0, "", ""
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(guard, "_docker", side_effect=fake), \
                mock.patch("os.makedirs"):
            guard.guard_start("/ws", out=io.StringIO())
        return calls

    def test_old_spec_is_recreated(self):
        calls = self._start("")
        self.assertIn(["rm", "-f", "kali-bounty"], calls)
        self.assertTrue(any(c[0] == "run" and "--read-only" in c for c in calls))
        self.assertFalse(any(c[0] == "volume" for c in calls))   # volumes are never touched

    def test_current_spec_is_restarted(self):
        calls = self._start(guard.SPEC)
        self.assertNotIn(["rm", "-f", "kali-bounty"], calls)
        self.assertIn(["start", "kali-bounty"], calls)


if __name__ == "__main__":
    unittest.main()
