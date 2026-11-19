"""BE-18 recon (CONTRACTS §10): passive merge, classification, the list, map preconditions.
No network: crt.sh and the resolver are fakes; hosts are .test names."""

import datetime
import importlib.util
import io
import time
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bounty import recon
from bounty.util import BountyError


def profile(handle="test-acme", ins=None, outs=None, rps=2, automation="allowed"):
    ins = ins or [("wildcard", "*.acme.test"), ("domain", "acme.test"), ("url", "https://shop.other.test/app")]
    outs = outs or [("domain", "community.acme.test")]
    return {"schema": 1, "handle": handle, "platform": "manual", "name": handle, "status": "open",
            "detail": "ok", "legal_basis": "authorization",
            "scope": {"in": [dict(type=t, value=v, tier="tier-1", description="") for t, v in ins],
                      "out": [dict(type=t, value=v, description="") for t, v in outs]},
            "rules": {"rate_limit_rps": rps, "automation": automation, "user_agent": None,
                      "request_header": None, "ai_use": "unknown"}}


CRT = json.dumps([
    {"name_value": "app.acme.test\n*.api.acme.test", "common_name": "app.acme.test"},
    {"name_value": "community.acme.test", "common_name": "x@acme.test"},
    {"name_value": "elsewhere.test", "common_name": "dead.acme.test"},
])


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws)
        self.write_profile(profile())

    def write_profile(self, p):
        d = os.path.join(self.ws, "programs", p["handle"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "program.json"), "w") as f:
            json.dump(p, f)

    def rdir(self, h="test-acme"):
        return recon.recon_dir(self.ws, h)

    def jsonl(self, name, h="test-acme"):
        with open(os.path.join(self.rdir(h), name)) as f:
            return [json.loads(l) for l in f if l.strip()]

    def put(self, name, records, h="test-acme"):
        os.makedirs(self.rdir(h), exist_ok=True)
        p = os.path.join(self.rdir(h), name)
        with open(p, "w") as f:
            if name.endswith(".json"):
                json.dump(records, f)
            else:
                f.write("".join(json.dumps(r) + "\n" for r in records))

    def passive(self, fetch=None, resolver=None, **kw):
        calls = {"fetch": [], "resolve": []}

        def f(domain):
            calls["fetch"].append(domain)
            return CRT

        def r(name):
            calls["resolve"].append(name)
            return (name != "dead.acme.test", ["203.0.113.10"] if name != "dead.acme.test" else [])
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()):
            code = recon.passive(self.ws, "test-acme", out=out, fetch=fetch or f, resolver=resolver or r,
                                 sleep=lambda s: None, **kw)
        return code, calls, out.getvalue()


class Passive(Base):
    def test_names_from_crtsh(self):
        self.assertEqual(recon.names_from_crtsh(CRT, "acme.test"),
                         ["api.acme.test", "app.acme.test", "community.acme.test", "dead.acme.test"])

    def test_domains_and_scope_hosts(self):
        p = profile()
        self.assertEqual(recon.passive_domains(p), ["acme.test"])
        self.assertEqual(recon.scope_hosts(p), ["acme.test", "shop.other.test"])

    def test_writes_classified_hosts_and_resolves_only_in_scope(self):
        code, calls, out = self.passive()
        self.assertEqual(code, 0)
        self.assertEqual(calls["fetch"], ["acme.test"])
        self.assertNotIn("community.acme.test", calls["resolve"])     # out of scope: never looked up
        hosts = {r["host"]: r for r in self.jsonl("hosts.jsonl")}
        self.assertEqual(hosts["community.acme.test"]["scope"], "out-of-scope")
        self.assertIsNone(hosts["community.acme.test"]["resolves"])
        self.assertEqual(hosts["app.acme.test"]["scope"], "in-scope")
        self.assertTrue(hosts["app.acme.test"]["resolves"])
        self.assertEqual(hosts["app.acme.test"]["ips"], ["203.0.113.10"])
        self.assertFalse(hosts["dead.acme.test"]["resolves"])
        self.assertEqual(hosts["shop.other.test"]["sources"], ["scope"])
        self.assertEqual(hosts["acme.test"]["sources"], ["scope"])
        self.assertIn("in scope", out)

    def test_merge_keeps_first_seen(self):
        self.passive()
        first = {r["host"]: r["first_seen"] for r in self.jsonl("hosts.jsonl")}
        self.put("hosts.jsonl", [dict(r, first_seen="2020-01-01T00:00:00Z") for r in self.jsonl("hosts.jsonl")])
        self.passive()
        again = {r["host"]: r for r in self.jsonl("hosts.jsonl")}
        self.assertEqual(set(again), set(first))
        self.assertTrue(all(r["first_seen"] == "2020-01-01T00:00:00Z" for r in again.values()))

    def test_crtsh_failure_keeps_partial_and_exits_3(self):
        def bad(_d):
            raise BountyError("crtsh", "crt.sh failed for acme.test: timeout", 3)
        with self.assertRaises(BountyError) as cm:
            self.passive(fetch=bad)
        self.assertEqual(cm.exception.exit_code, 3)
        self.assertEqual({r["host"] for r in self.jsonl("hosts.jsonl")}, {"acme.test", "shop.other.test"})

    def test_fetch_retries_once(self):
        n = {"calls": 0}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            n["calls"] += 1
            self.assertEqual(timeout, recon.CRTSH_TIMEOUT)
            self.assertEqual(req.get_header("User-agent"), recon.USER_AGENT)
            self.assertIn("q=%25.acme.test", req.full_url)
            if n["calls"] == 1:
                raise OSError("reset")
            return Resp(b"[]")
        self.assertEqual(recon.fetch_crtsh("acme.test", opener=opener, sleep=lambda s: None), "[]")
        self.assertEqual(n["calls"], 2)

    def test_domain_must_be_in_scope(self):
        with self.assertRaises(BountyError) as cm:
            self.passive(domain="evil.test")
        self.assertEqual(cm.exception.exit_code, 64)

    def test_bad_handle(self):
        with self.assertRaises(BountyError):
            recon.passive(self.ws, "../x", fetch=None)


class List(Base):
    NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)

    def test_rows_merge_and_rank(self):
        self.put("state.json", {"baseline_at": "2026-11-01T00:00:00Z"})
        self.put("hosts.jsonl", [
            {"host": "old.acme.test", "scope": "in-scope", "first_seen": "2026-11-01T00:00:00Z", "resolves": True},
            {"host": "new.acme.test", "scope": "in-scope", "first_seen": "2026-11-17T00:00:00Z", "resolves": True},
            {"host": "api.acme.test", "scope": "in-scope", "first_seen": "2026-11-01T00:00:00Z", "resolves": True},
            {"host": "silent.acme.test", "scope": "in-scope", "first_seen": "2026-11-01T00:00:00Z", "resolves": True},
            {"host": "community.acme.test", "scope": "out-of-scope", "first_seen": "2026-11-01T00:00:00Z"},
        ])
        self.put("live.jsonl", [
            {"host": "api.acme.test", "url": "http://api.acme.test", "status": None, "refused": "rate-limited"},
            {"host": "api.acme.test", "url": "https://api.acme.test", "status": 200, "title": "Admin console",
             "tech": ["React"], "final_url": "https://api.acme.test/"},
            {"host": "old.acme.test", "url": "https://old.acme.test", "status": 302,
             "location": "https://login.elsewhere.test/"},
        ])
        self.put("urls.jsonl", [
            {"host": "api.acme.test", "url": "https://api.acme.test/graphql", "method": "POST"},
            {"host": "api.acme.test", "url": "https://api.acme.test/graphql", "method": "POST"},
            {"host": "api.acme.test", "url": "https://api.acme.test/upload", "method": "GET"},
        ])
        self.put("mapped.json", {"hosts": {"api.acme.test": "x", "old.acme.test": "x", "silent.acme.test": "x"}})
        rows = {r["host"]: r for r in recon.rows(self.ws, "test-acme", now=self.NOW)}
        api = rows["api.acme.test"]
        self.assertEqual((api["live"], api["status"], api["title"], api["urls"]), (True, 200, "Admin console", 2))
        self.assertEqual(set(api["signals"]), {"api", "admin", "graphql", "upload", "js-heavy"})
        self.assertFalse(api["new"])
        self.assertEqual(api["interest"], 0.65)
        self.assertTrue(rows["new.acme.test"]["new"])
        self.assertIsNone(rows["new.acme.test"]["live"])           # never mapped
        self.assertFalse(rows["silent.acme.test"]["live"])         # mapped, no answer
        self.assertIn("redirect-offsite", rows["old.acme.test"]["signals"])
        self.assertEqual(rows["community.acme.test"]["scope"], "out-of-scope")
        order = [r["host"] for r in recon.rows(self.ws, "test-acme", now=self.NOW)]
        self.assertEqual(order[0], "api.acme.test")
        self.assertLess(order.index("new.acme.test"), order.index("silent.acme.test"))
        self.assertEqual(set(api), {"host", "scope", "new", "live", "status", "title", "tech", "urls",
                                    "signals", "exposures", "interest"})

    def test_exposures_score_and_show(self):
        self.put("hosts.jsonl", [{"host": "old.acme.test", "scope": "in-scope", "first_seen": "2026-11-01T00:00:00Z"}])
        self.put("live.jsonl", [{"host": "old.acme.test", "url": "https://old.acme.test", "status": 200}])
        self.put("exposures.json", {"checked": {"old.acme.test": "x"}, "hits": [
            {"host": "old.acme.test", "check": "kl-git-config"}, {"host": "old.acme.test", "check": "kl-env-file"},
            {"host": "old.acme.test", "check": "kl-env-file"}]})
        r = recon.rows(self.ws, "test-acme", now=self.NOW)[0]
        self.assertEqual(r["exposures"], ["kl-env-file", "kl-git-config"])
        self.assertIn("exposed", r["signals"])
        self.assertEqual(r["interest"], 0.45)        # live 0.15 + exposure 0.30 ("exposed" isn't counted twice)

    def test_signal_edge_cases(self):
        self.assertIn("login", recon.signals_for("www.acme.test", {"title": "Dropbox Paper — Sign in"}, []))
        self.assertIn("login", recon.signals_for("www.acme.test", {"title": "Log in to Acme"}, []))
        self.assertNotIn("login", recon.signals_for("www.acme.test", {"title": "Our design into practice"}, []))
        # a two-level public suffix: the company label isn't a subdomain word
        self.assertNotIn("debug", recon.signals_for("app.staging.co.uk", None, []))
        self.assertIn("debug", recon.signals_for("staging.acme.co.uk", None, []))
        self.assertEqual(recon._registrable("a.b.acme.co.uk"), "acme.co.uk")
        self.assertIn("redirect-offsite", recon.signals_for("a.acme.co.uk", {"location": "https://evil.co.uk/"}, []))
        self.assertNotIn("redirect-offsite", recon.signals_for("a.acme.co.uk", {"location": "https://b.acme.co.uk/"}, []))

    def test_first_run_is_not_new(self):
        self.passive()
        rows = recon.rows(self.ws, "test-acme")
        self.assertTrue(rows)
        self.assertFalse(any(r["new"] for r in rows))

    def test_broken_line_is_skipped(self):
        os.makedirs(self.rdir(), exist_ok=True)
        with open(os.path.join(self.rdir(), "hosts.jsonl"), "w") as f:
            f.write('{"host": "a.acme.test", "scope": "in-scope"}\n{"host": "b.ac')
        self.assertEqual([r["host"] for r in recon.rows(self.ws, "test-acme")], ["a.acme.test"])

    def test_json_output(self):
        out = io.StringIO()
        self.assertEqual(recon.list_cmd(self.ws, "test-acme", True, out=out), 0)
        self.assertEqual(json.loads(out.getvalue()), [])


def _bounty_map():
    import importlib.machinery
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(root, "container", "share", "bounty"))
    loader = importlib.machinery.SourceFileLoader("bounty_map", os.path.join(root, "container", "bin", "bounty-map"))
    spec = importlib.util.spec_from_loader("bounty_map", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class MapOrder(unittest.TestCase):
    def test_interest_order_then_skip_fresh(self):
        bm = _bounty_map()
        todo = [("a.acme.test", None), ("b.acme.test", None), ("c.acme.test", None), ("d.acme.test", None)]
        now = 1_800_000_000
        mapped = {"b.acme.test": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),        # fresh
                  "c.acme.test": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30 * 86400))}  # stale
        got, skipped = bm.order_and_skip(todo, ["c.acme.test", "b.acme.test"], mapped, False, now)
        self.assertEqual([t[0] for t in got], ["c.acme.test", "a.acme.test", "d.acme.test"])
        self.assertEqual(skipped, 1)
        got, skipped = bm.order_and_skip(todo, ["c.acme.test", "b.acme.test"], mapped, True, now)
        self.assertEqual(([t[0] for t in got], skipped), (["c.acme.test", "b.acme.test", "a.acme.test", "d.acme.test"], 0))


class Map(Base):
    def test_args(self):
        self.assertEqual(recon.map_args("kali-bounty", "test-acme", 5),
                         ["exec", "-e", "BOUNTY_HANDLE=test-acme", "-w", "/bounty/work/test-acme", "kali-bounty",
                          "bounty-run", "--actor", "human", "--", "bounty-map", "--limit", "5"])
        self.assertNotIn("claude", recon.map_args("kali-bounty", "test-acme"))

    def _map(self, running=True, active="test-acme"):
        ran = []
        with mock.patch.object(recon.guard, "running", return_value=running), \
                mock.patch.object(recon.shell, "active_program", return_value=active), \
                mock.patch.object(recon.guard, "_docker", return_value=(0, "", "")):
            code = recon.map_cmd(self.ws, run=lambda a: ran.append(a) or 0)
        return code, ran

    def test_all_flag_and_order_file(self):
        self.assertEqual(recon.map_args("kali-bounty", "test-acme", None, redo_all=True)[-1], "--all")
        self.assertNotIn("--all", recon.map_args("kali-bounty", "test-acme", None, "bounty-exposures", True))
        self.put("hosts.jsonl", [{"host": "admin.acme.test", "scope": "in-scope", "first_seen": "2026-01-01T00:00:00Z"},
                                 {"host": "www.acme.test", "scope": "in-scope", "first_seen": "2026-01-01T00:00:00Z"}])
        self._map()
        with open(os.path.join(self.rdir(), "map-order.json")) as f:
            self.assertEqual(json.load(f)["hosts"][0], "admin.acme.test")

    def test_stop(self):
        with mock.patch.object(recon.guard, "running", return_value=True), \
                mock.patch.object(recon.guard, "_docker", return_value=(0, "", "")) as d:
            self.assertEqual(recon.stop_cmd(out=io.StringIO()), 0)
        self.assertEqual(d.call_args[0][0][-6:], ["pkill", "-TERM", "-u", "hunter", "-f", recon.STOP_PATTERN])
        with mock.patch.object(recon.guard, "running", return_value=True), \
                mock.patch.object(recon.guard, "_docker", return_value=(1, "", "")):
            self.assertEqual(recon.stop_cmd(out=io.StringIO()), 1)

    def test_guard_down_exits_3(self):
        with self.assertRaises(BountyError) as cm:
            self._map(running=False)
        self.assertEqual(cm.exception.exit_code, 3)

    def test_no_program_exits_5(self):
        with self.assertRaises(BountyError) as cm:
            self._map(active=None)
        self.assertEqual(cm.exception.exit_code, 5)

    def test_automation_forbidden_exits_6_before_docker(self):
        self.write_profile(profile(rps=0, automation="forbidden"))
        with self.assertRaises(BountyError) as cm:
            self._map()
        self.assertEqual(cm.exception.exit_code, 6)

    def test_exposures_same_gates_other_script(self):
        ran = []
        with mock.patch.object(recon.guard, "running", return_value=True), \
                mock.patch.object(recon.shell, "active_program", return_value="test-acme"), \
                mock.patch.object(recon.guard, "_docker", return_value=(0, "", "")):
            recon.exposures_cmd(self.ws, 3, run=lambda a: ran.append(a) or 0)
        self.assertEqual(ran[0][-3:], ["bounty-exposures", "--limit", "3"])
        self.write_profile(profile(rps=0, automation="forbidden"))
        with self.assertRaises(BountyError) as cm:
            with mock.patch.object(recon.guard, "running", return_value=True), \
                    mock.patch.object(recon.shell, "active_program", return_value="test-acme"):
                recon.exposures_cmd(self.ws, run=lambda a: self.fail("must not run"))
        self.assertEqual(cm.exception.exit_code, 6)

    def test_runs_only_the_active_program(self):
        code, ran = self._map()
        self.assertEqual(code, 0)
        self.assertEqual(ran[0][:2], ["docker", "exec"])
        self.assertIn("BOUNTY_HANDLE=test-acme", ran[0])


if __name__ == "__main__":
    unittest.main()
