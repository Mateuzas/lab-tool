"""BE-19 (CONTRACTS §13): access-control check logic (ac_core, by path) and the Mac-side gates."""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bounty import ac
from bounty.util import BountyError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _core():
    p = os.path.join(ROOT, "container", "share", "bounty", "ac_core.py")
    spec = importlib.util.spec_from_file_location("ac_core", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


core = _core()
A = [["Cookie", "sid=aaaaaaaaaaaaaaaaaaaa; theme=dark"], ["X-Csrf", "tokA"]]
B = [["Cookie", "sid=bbbbbbbbbbbbbbbbbbbb"], ["X-Csrf", "tokB"]]


class Session(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(core.parse_session("# A\nCookie: sid=x\n\nAuthorization:  Bearer abc \n"),
                         [["Cookie", "sid=x"], ["Authorization", "Bearer abc"]])

    def test_parse_refuses(self):
        for bad in ("", "\n# only a comment\n", "not a header", "Host: a.test", "Content-Length: 3", "X-Empty:"):
            with self.assertRaises(core.SessionError, msg=bad):
                core.parse_session(bad)

    def test_matches(self):
        req = [["Host", "a.test"], ["Cookie", "theme=dark; sid=aaaaaaaaaaaaaaaaaaaa"]]
        self.assertTrue(core.matches(A, req))
        self.assertFalse(core.matches(B, req))
        self.assertFalse(core.matches(A, [["Cookie", "theme=dark"]]))      # short cookie isn't a session
        self.assertTrue(core.matches([["Authorization", "Bearer t"]], [["authorization", "Bearer t"]]))
        self.assertFalse(core.matches([["Authorization", "Bearer t"]], [["Authorization", "Bearer u"]]))

    def test_rewrite_swaps_sessions(self):
        req = [["Host", "a.test"], ["Cookie", "sid=aaaaaaaaaaaaaaaaaaaa"], ["X-Csrf", "tokA"],
               ["Accept-Encoding", "gzip"], ["Connection", "keep-alive"], ["X-Bounty-Actor", "human"], ["Accept", "*/*"]]
        as_b = core.rewrite(req, (A, B), B)
        self.assertEqual(as_b, [["Host", "a.test"], ["Accept", "*/*"], ["Cookie", "sid=bbbbbbbbbbbbbbbbbbbb"],
                                ["X-Csrf", "tokB"], ["Accept-Encoding", "identity"]])
        anon = core.rewrite(req, (A, B), None)
        self.assertFalse(any(k.lower() in ("cookie", "x-csrf") for k, _ in anon))
        self.assertNotIn("aaaaaaaa", json.dumps(anon))


class Judge(unittest.TestCase):
    A_BODY = b'{"owner":"alice@a.test","id":"123456789","uuid":"0f8fad5b-d9cb-469f-a165-70867728950e","doc":"secret plans"}'

    def test_verdicts(self):
        j = core.judge
        url = "https://a.test/doc/42"
        self.assertEqual(j(200, self.A_BODY, url, [], 403, b"no")["verdict"], "blocked")
        self.assertEqual(j(200, self.A_BODY, url, [], 302, b"")["verdict"], "blocked")
        self.assertEqual(j(200, self.A_BODY, url, [], 500, b"err")["verdict"], "blocked")
        self.assertEqual(j(200, self.A_BODY, url, [], 200, self.A_BODY)["verdict"], "same-as-a")
        self.assertEqual(j(200, self.A_BODY, url, ["alice@a.test"], 200, b'{"x":"alice@a.test"}')["verdict"], "leak")
        self.assertEqual(j(200, self.A_BODY, url, [], 200, b'{"ref":"123456789","other":"zzzz"}')["verdict"], "a-data")
        self.assertEqual(j(200, self.A_BODY, url, [], 200, b'{"owner":"bob@b.test","doc":"his"}')["verdict"], "differs")
        r = j(200, self.A_BODY, url, [], None, b"", refused="out-of-scope")
        self.assertEqual((r["verdict"], r["refused"]), ("refused", "out-of-scope"))
        self.assertEqual(j(200, self.A_BODY, url, [], None, b"")["verdict"], "error")

    def test_ids_in_url_dont_count(self):
        body = b'{"doc":"42424242"}'
        self.assertEqual(core.a_ids(body, "https://a.test/doc/42424242", body), 0)

    def test_record_keeps_no_body(self):
        r = core.judge(200, self.A_BODY, "https://a.test/x", [], 200, self.A_BODY)
        self.assertEqual(set(r), {"status", "length", "similarity", "markers", "a_ids", "verdict"})
        self.assertNotIn("alice", json.dumps(r))

    def test_hint(self):
        self.assertEqual(core.hint("same-as-a", "blocked"), "likely-broken-access")
        self.assertEqual(core.hint("a-data", "differs"), "likely-broken-access")
        self.assertEqual(core.hint("same-as-a", "same-as-a"), "public")
        self.assertEqual(core.hint("blocked", "blocked"), "none")
        self.assertEqual(core.hint("differs", "blocked"), "none")

    def test_helpers(self):
        self.assertTrue(core.is_safe("get") and core.is_safe("HEAD") and not core.is_safe("POST"))
        self.assertTrue(core.is_static("https://a.test/app.js?v=1") and not core.is_static("https://a.test/api/me"))
        self.assertEqual(core.redact_url("https://a.test/x?id=7&access_token=abc&sig=zz"),
                         "https://a.test/x?id=7&access_token=***&sig=***")
        self.assertEqual(core.redact_url("https://a.test/x"), "https://a.test/x")
        self.assertEqual(core.similarity(b"", b""), 1.0)


class MacSide(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws)

    def profile(self, rps=2, automation="allowed"):
        d = os.path.join(self.ws, "programs", "test-acme")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "program.json"), "w") as f:
            json.dump({"schema": 1, "handle": "test-acme", "platform": "manual", "status": "open", "detail": "ok",
                       "scope": {"in": [{"type": "wildcard", "value": "*.acme.test"}], "out": []},
                       "rules": {"rate_limit_rps": rps, "automation": automation}}, f)

    def test_guard_down_3_no_program_5(self):
        with mock.patch.object(ac.guard, "running", return_value=False):
            with self.assertRaises(BountyError) as cm:
                ac.status(self.ws, out=io.StringIO())
        self.assertEqual(cm.exception.exit_code, 3)
        with mock.patch.object(ac.guard, "running", return_value=True), \
                mock.patch.object(ac.shell, "active_program", return_value=None):
            with self.assertRaises(BountyError) as cm:
                ac.status(self.ws, out=io.StringIO())
        self.assertEqual(cm.exception.exit_code, 5)

    def test_session_values_only_on_stdin(self):
        calls = []

        def fake(args, check=False, capture=True, env=None, input_text=None):
            calls.append((args, input_text))
            return 0, '{"names": ["Cookie"], "markers": 1}', ""
        with mock.patch.object(ac.guard, "running", return_value=True), \
                mock.patch.object(ac.shell, "active_program", return_value="test-acme"), \
                mock.patch.object(ac.guard, "_docker", side_effect=fake):
            out = io.StringIO()
            ac.session(self.ws, "a", ["alice@a.test"], stdin=io.StringIO("Cookie: sid=SECRETVALUE1234567\n"), out=out)
        args, stdin = calls[0]
        self.assertIn("-i", args)
        self.assertNotIn("SECRETVALUE1234567", " ".join(args))
        self.assertIn("SECRETVALUE1234567", stdin)
        self.assertNotIn("SECRETVALUE", out.getvalue())
        self.assertIn("Cookie", out.getvalue())

    def test_replay_refused_when_automation_forbidden(self):
        self.profile(rps=0, automation="forbidden")
        with mock.patch.object(ac.guard, "running", return_value=True), \
                mock.patch.object(ac.shell, "active_program", return_value="test-acme"), \
                mock.patch.object(ac.guard, "_docker", side_effect=AssertionError("no docker")):
            with self.assertRaises(BountyError) as cm:
                ac.replay(self.ws, ["x"], run=lambda *a, **k: self.fail("must not run"))
        self.assertEqual(cm.exception.exit_code, 6)

    def test_replay_args(self):
        self.profile()
        ran = []

        class R:
            returncode = 0
            stdout = b"[]"
        with mock.patch.object(ac.guard, "running", return_value=True), \
                mock.patch.object(ac.shell, "active_program", return_value="test-acme"), \
                mock.patch.object(ac.guard, "_docker", return_value=(0, "", "")):
            ac.replay(self.ws, ["f1", "f2"], ["f2"], run=lambda a, **k: ran.append(a) or R(), out=io.StringIO())
        self.assertEqual(ran[0][-6:], ["--handle", "test-acme", "--ids", "f1,f2", "--unsafe", "f2"])
        self.assertNotIn("bounty-run", ran[0])     # root helper; it drops to hunter itself

    def test_results_flagged_first(self):
        d = os.path.join(self.ws, "work", "test-acme", "ac")
        os.makedirs(d)
        with open(os.path.join(d, "results.jsonl"), "w") as f:
            for i, h in enumerate(("none", "likely-broken-access", "public")):
                f.write(json.dumps({"ts": "2026-11-18T00:00:0%dZ" % i, "hint": h, "method": "GET", "url": "u%d" % i,
                                    "a": {"status": 200}, "b": {}, "none": {}}) + "\n")
        out = io.StringIO()
        ac.results(self.ws, "test-acme", True, out=out)
        self.assertEqual([r["hint"] for r in json.loads(out.getvalue())], ["likely-broken-access", "public", "none"])


if __name__ == "__main__":
    unittest.main()
