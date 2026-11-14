import contextlib
import datetime
import io
import os
import shutil
import tempfile
import unittest
import urllib.parse
from unittest import mock

from bounty import cli, intigriti, sync
from bounty.intigriti import Client, Response
from bounty.util import BountyError, read_json, read_jsonl
from bounty.tests import fixtures as fx

TOKEN = "-".join(["synthetic", "intigriti", "test", "value", "7"])  # not a real token
NOW = datetime.datetime(2026, 11, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)


class FakeAPI(object):
    """Serves /v1/programs (paged by `page`), /v1/programs/activities and details."""

    def __init__(self, records, details, activities, page=None):
        self.records, self.details, self.activities = records, details, activities
        self.page = page
        self.calls = []
        self.fail = {}  # path -> list of statuses to return first

    def __call__(self, method, url, headers):
        u = urllib.parse.urlsplit(url)
        assert u.scheme == "https" and u.hostname == "api.intigriti.com", url
        assert headers["Authorization"] == "Bearer " + TOKEN
        assert headers["User-Agent"] == "kali-lab-bounty/0.1"
        path = u.path[len("/external/researcher"):]
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append(path + ("?" + u.query if u.query else ""))
        if self.fail.get(path):
            return Response(self.fail[path].pop(0), b"oops", {"Retry-After": "1"})
        if path == "/v1/programs":
            off, lim = int(q["offset"]), int(q["limit"])
            lim = min(lim, self.page or lim)
            return Response(200, fx.body({"maxCount": len(self.records), "records": self.records[off:off + lim]}))
        if path == "/v1/programs/activities":
            off, lim = int(q["offset"]), int(q["limit"])
            lim = min(lim, self.page or lim)
            acts = sorted(self.activities, key=lambda a: -a["createdAt"])
            return Response(200, fx.body({"maxCount": len(acts), "records": acts[off:off + lim]}))
        pid = path.rsplit("/", 1)[1]
        if pid in self.details:
            return Response(200, fx.body(self.details[pid]))
        return Response(403, b'{"message":"You need to join this program"}')


def make_client(api, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    clock = [0.0]

    def sleep(s):
        sleeps.append(s)
        clock[0] += s
    return Client(TOKEN, transport=api, sleep=sleep, clock=lambda: clock[0])


def world():
    a = fx.list_record(pid="p-a", handle="acme")
    b = fx.list_record(pid="p-b", handle="beta", conf="Registered", max_b=2000, currency="USD")
    c = fx.list_record(pid="p-c", handle="gone", status="Suspended")
    details = {"p-a": fx.detail(a)}
    acts = [fx.activity("p-a", 1700000000), fx.activity("p-a", 1600000000, 4, "New domains version added"),
            fx.activity("p-b", 1750000000)]
    return [a, b, c], details, acts


class TestClient(unittest.TestCase):
    def test_retry_then_success_and_rate_limit(self):
        recs, det, acts = world()
        api = FakeAPI(recs, det, acts)
        api.fail["/v1/programs"] = [429, 503]
        sleeps = []
        c = make_client(api, sleeps)
        _, body = c.programs_page(500, 0)
        self.assertEqual(len(body["records"]), 3)
        self.assertEqual(len(api.calls), 3)
        self.assertGreaterEqual(min(sleeps), 1.0)  # every gap ≥ 1 s

    def test_gives_up_after_three_retries(self):
        api = FakeAPI(*world())
        api.fail["/v1/programs"] = [500, 500, 500, 500]
        with self.assertRaises(BountyError) as e:
            make_client(api).programs_page(500, 0)
        self.assertEqual(e.exception.exit_code, 3)
        self.assertEqual(len(api.calls), 4)

    def test_401_is_auth(self):
        api = FakeAPI(*world())
        api.fail["/v1/programs"] = [401]
        with self.assertRaises(BountyError) as e:
            make_client(api).programs_page(500, 0)
        self.assertEqual(e.exception.exit_code, 2)
        self.assertNotIn(TOKEN, e.exception.message)

    def test_network_error_retried(self):
        n = [0]

        def transport(m, u, h):
            n[0] += 1
            raise OSError("connection reset")
        c = Client(TOKEN, transport=transport, sleep=lambda s: None)
        with self.assertRaises(BountyError) as e:
            c.get("/v1/programs")
        self.assertEqual((e.exception.exit_code, n[0]), (3, 4))

    def test_only_api_host(self):
        c = Client(TOKEN, transport=lambda *a: None, sleep=lambda s: None)
        with mock.patch.object(intigriti, "BASE_URL", "https://evil.example/x"):
            with self.assertRaises(BountyError):
                c.get("/v1/programs")

    def test_missing_token(self):
        with self.assertRaises(BountyError) as e:
            Client("")
        self.assertEqual(e.exception.exit_code, 2)


class TestSync(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws)

    def test_full_sync(self):
        recs, det, acts = world()
        api = FakeAPI(recs, det, acts, page=2)  # forces offset paging on both lists
        idx = sync.run(make_client(api), self.ws, now=NOW, log=lambda m: None)
        self.assertEqual(idx["ts"], "20261113T120000Z")
        self.assertEqual(idx["counts"], {"programs": 3, "open": 2, "detail_ok": 1, "needs_join": 1,
                                         "detail_errors": 0, "not_fetched": 1, "detail_kept": 0,
                                         "activities_new": 3, "details_planned": 2})
        self.assertIn("/v1/programs?limit=500&offset=2", api.calls)
        self.assertNotIn("/v1/programs/p-c", api.calls)  # suspended: no detail request
        P = lambda h: read_json(os.path.join(self.ws, "programs", h, "program.json"))
        a, b = P("intigriti-acme"), P("intigriti-beta")
        self.assertEqual(a["detail"], "ok")
        self.assertEqual(a["first_seen_at"], "2020-09-13T12:26:40Z")
        self.assertEqual(b["detail"], "needs-join")
        self.assertIsNone(b["scope"])
        self.assertEqual(b["bounty"]["max_eur"], 1840)
        raw = os.path.join(self.ws, "programs", "intigriti-acme", "raw", idx["ts"])
        self.assertEqual(sorted(os.listdir(raw)), ["detail.json", "list.json"])
        self.assertTrue(os.path.exists(os.path.join(self.ws, "programs", "intigriti-beta", "raw", idx["ts"],
                                                    "detail-403.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.ws, "programs", "intigriti-acme", "snapshots",
                                                    idx["ts"] + ".json")))
        self.assertTrue(os.path.exists(os.path.join(self.ws, "config", "ranking.json")))
        self.assertTrue(os.path.exists(os.path.join(self.ws, "sync", idx["ts"] + ".json")))

    def test_activities_incremental(self):
        recs, det, acts = world()
        api = FakeAPI(recs, det, acts, page=1)
        sync.run(make_client(api), self.ws, now=NOW, log=lambda m: None)
        api.activities.append(fx.activity("p-b", 1800000000))
        api.calls = []
        idx = sync.run(make_client(api), self.ws, now=NOW + datetime.timedelta(hours=1), log=lambda m: None)
        self.assertEqual(idx["counts"]["activities_new"], 1)
        act_calls = [c for c in api.calls if c.startswith("/v1/programs/activities")]
        self.assertEqual(len(act_calls), 2)  # new page, then the page with a known event: stop
        stored = read_jsonl(os.path.join(self.ws, "activities.jsonl"))
        self.assertEqual(len(stored), 4)
        self.assertEqual([s["createdAt"] for s in stored][-1], 1800000000)

    def test_handle_only(self):
        api = FakeAPI(*world())
        idx = sync.run(make_client(api), self.ws, handle="acme", now=NOW, log=lambda m: None)
        self.assertEqual(idx["handles"], ["intigriti-acme"])
        self.assertTrue(idx["partial"])
        with self.assertRaises(BountyError) as e:
            sync.run(make_client(api), self.ws, handle="nope", now=NOW, log=lambda m: None)
        self.assertEqual(e.exception.exit_code, 64)

    def test_detail_server_error_is_recorded(self):
        api = FakeAPI(*world())
        api.fail["/v1/programs/p-a"] = [500, 500, 500, 500]
        idx = sync.run(make_client(api), self.ws, now=NOW, log=lambda m: None)
        self.assertEqual(idx["counts"]["detail_errors"], 1)
        self.assertEqual(idx["errors"][0]["handle"], "intigriti-acme")


class TestTokenNeverLeaks(unittest.TestCase):
    def run_cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue() + err.getvalue()

    def test_token_not_in_output_or_files(self):
        ws = tempfile.mkdtemp()
        try:
            recs, det, acts = world()
            api = FakeAPI(recs, det, acts)
            real = Client.__init__

            def init(self, token, transport=None, sleep=None, clock=None, log=None):
                real(self, token, transport=api, sleep=lambda s: None, log=log)
            env = {"INTIGRITI_TOKEN": TOKEN, "BOUNTY_WORKSPACE": ws}
            outputs = []
            with mock.patch.object(Client, "__init__", init):
                outputs.append(self.run_cli(["programs", "sync"], env))
                api.fail["/v1/programs"] = [401]
                outputs.append(self.run_cli(["programs", "sync"], env))
                api.fail["/v1/programs"] = [500, 500, 500, 500]
                outputs.append(self.run_cli(["programs", "sync"], env))
            outputs.append(self.run_cli(["programs", "rank", "--json"], env))
            outputs.append(self.run_cli(["programs", "rank", "--explain", "acme"], env))
            outputs.append(self.run_cli(["programs", "list"], env))
            outputs.append(self.run_cli(["programs", "changes"], env))
            self.assertEqual([o[0] for o in outputs], [0, 2, 3, 0, 0, 0, 1])
            for _, text in outputs:
                self.assertNotIn(TOKEN, text)
                self.assertNotIn("Bearer", text)
            for root, _, files in os.walk(ws):
                for f in files:
                    with open(os.path.join(root, f), "rb") as fh:
                        self.assertNotIn(TOKEN.encode(), fh.read(), f)
        finally:
            shutil.rmtree(ws)

    def test_missing_token_message(self):
        with mock.patch.dict(os.environ, {"BOUNTY_WORKSPACE": tempfile.gettempdir()}):
            os.environ.pop("INTIGRITI_TOKEN", None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(["programs", "sync"])
        self.assertEqual(rc, 2)
        self.assertEqual(err.getvalue().strip(), "bounty: auth: INTIGRITI_TOKEN not set "
                         "(run via scripts/secrets.sh exec intigriti -- host/bounty …)")


if __name__ == "__main__":
    unittest.main()
