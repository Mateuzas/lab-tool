import datetime
import json
import math
import os
import shutil
import tempfile
import unittest

from bounty import rank as R
from bounty.config import DEFAULT_RANKING, load_ranking

NOW = datetime.datetime(2026, 11, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
W = DEFAULT_RANKING["weights"]


def days_ago(d):
    return (NOW - datetime.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prog(handle, max_eur=1000, min_eur=100, conf="public", detail="ok", entries=None, rps=5,
         safe_harbour=True, intigriti_me=False, user_agent=None, scope_days=0, first_days=730,
         status="open", request_header=None):
    if entries is None:
        entries = [{"type": "wildcard", "value": "*.x.example", "tier": "tier-1"}]
    p = {"handle": handle, "name": handle, "status": status, "confidentiality": conf, "detail": detail,
         "first_seen_at": days_ago(first_days) if first_days is not None else None,
         "bounty": {"min": min_eur, "max": max_eur, "currency": "EUR", "min_eur": min_eur, "max_eur": max_eur},
         "scope": None, "rules": None}
    if detail == "ok":
        p["scope"] = {"version": "v", "changed_at": days_ago(scope_days) if scope_days is not None else None,
                      "in": entries, "out": []}
        p["rules"] = {"safe_harbour": safe_harbour, "rate_limit_rps": rps,
                      "automation": "unknown" if rps is None else ("forbidden" if rps == 0 else "allowed"),
                      "intigriti_me": intigriti_me, "user_agent": user_agent, "request_header": request_header}
    return p


def by_handle(results):
    return dict((r["handle"], r) for r in results)


class TestPercentile(unittest.TestCase):
    def test_pr(self):
        pr = R.percentile_ranker([1, 2, 3, 4, 5])
        self.assertEqual(pr(1), 0.0)
        self.assertEqual(pr(5), 1.0)
        self.assertEqual(pr(3), 0.5)
        pr = R.percentile_ranker([1, 1, 3])
        self.assertEqual(pr(1), 0.25)
        self.assertEqual(R.percentile_ranker([7])(7), 0.5)
        self.assertIsNone(pr(None))

    def test_wavg_renormalises(self):
        self.assertAlmostEqual(R.wavg([(0.7, 1.0), (0.3, None)]), 1.0)
        self.assertAlmostEqual(R.wavg([(0.5, 1.0), (0.25, None), (0.25, 0.0)]), 2.0 / 3)
        self.assertIsNone(R.wavg([(1, None)]))


class TestFactors(unittest.TestCase):
    def test_payout(self):
        progs = [prog("a", 1000, 100, entries=[{"type": "wildcard", "value": "*.a", "tier": "tier-1"},
                                                {"type": "url", "value": "https://a/", "tier": "no-bounty"}]),
                 prog("b", 5000, 100), prog("c", 100, 10)]
        res = by_handle(R.score(progs, W, NOW))
        self.assertAlmostEqual(res["b"]["factors"]["payout"], 1.0)        # top pr, paid_share 1
        self.assertAlmostEqual(res["a"]["factors"]["payout"], 0.5 * 0.75)  # mid pr, paid_share .5
        self.assertAlmostEqual(res["c"]["factors"]["payout"], 0.0)
        self.assertAlmostEqual(res["a"]["inputs"]["paid_share"], 0.5)

    def test_findability(self):
        many = [{"type": "wildcard", "value": "*.%d" % i, "tier": "tier-2"} for i in range(3)]
        few = [{"type": "url", "value": "https://one/", "tier": "tier-2"},
               {"type": "android", "value": "com.x", "tier": "tier-2"},
               {"type": "other", "value": "misc", "tier": "tier-2"}]
        progs = [prog("big", entries=many, scope_days=0), prog("small", entries=few, scope_days=90)]
        res = by_handle(R.score(progs, W, NOW))
        self.assertEqual(res["big"]["inputs"]["surface"], 15)
        self.assertEqual(res["small"]["inputs"]["surface"], 1.5)
        self.assertAlmostEqual(res["big"]["factors"]["findability"], 0.7 * 1 + 0.3 * 1.0)
        self.assertAlmostEqual(res["small"]["factors"]["findability"], 0.3 * math.exp(-1))

    def test_findability_missing_freshness(self):
        res = by_handle(R.score([prog("a", scope_days=None), prog("b", scope_days=None,
                                 entries=[{"type": "domain", "value": "b", "tier": "tier-1"}])], W, NOW))
        self.assertAlmostEqual(res["a"]["factors"]["findability"], 1.0)  # pr only, weight renormalised

    def test_competition(self):
        progs = [prog("pub", conf="public", max_eur=1000, first_days=730),
                 prog("app", conf="application", max_eur=500, first_days=365),
                 prog("new", conf="registered", max_eur=700, first_days=None)]
        res = by_handle(R.score(progs, W, NOW))
        self.assertAlmostEqual(res["pub"]["factors"]["competition"], 0.5 * 1 + 0.25 * 1 + 0.25 * 1)
        self.assertAlmostEqual(res["app"]["factors"]["competition"], 0.5 * 0.4 + 0.25 * 0.5 + 0.25 * 0)
        # age missing -> (0.5·0.7 + 0.25·0.5) / 0.75
        self.assertAlmostEqual(res["new"]["factors"]["competition"], (0.35 + 0.125) / 0.75)

    def test_friction(self):
        cases = {"forbidden": (0, 0.6), "slow": (2, 0.3), "unknown": (None, 0.15), "fast": (10, 0.0)}
        progs = [prog(h, rps=v[0]) for h, v in cases.items()]
        progs.append(prog("all", rps=0, intigriti_me=True, user_agent="Intigriti-{Username}"))
        res = by_handle(R.score(progs, W, NOW))
        for h, (_, f) in cases.items():
            self.assertAlmostEqual(res[h]["factors"]["friction"], f, msg=h)
        self.assertAlmostEqual(res["all"]["factors"]["friction"], 0.7)
        self.assertIn("no-auto", res["forbidden"]["flags"])

    def test_total_formula(self):
        res = R.score([prog("a"), prog("b", max_eur=2000)], W, NOW)
        for r in res:
            f = r["factors"]
            want = (W["payout"] * f["payout"] + W["findability"] * f["findability"]
                    + W["competition"] * (1 - f["competition"]) - W["friction"] * f["friction"])
            self.assertAlmostEqual(r["total"], want)


class TestFilters(unittest.TestCase):
    def test_hard_filters(self):
        progs = [prog("ok"), prog("nobounty", max_eur=0, min_eur=0), prog("nosh", safe_harbour=False),
                 prog("noweb", entries=[{"type": "android", "value": "com.x", "tier": "tier-1"},
                                        {"type": "cidr", "value": "10.0.0.0/8", "tier": "tier-1"}]),
                 prog("closed", status="suspended")]
        res = R.score(progs, W, NOW)
        ex = dict((r["handle"], r["excluded"]) for r in res)
        self.assertEqual(ex, {"ok": None, "nobounty": "no-bounty", "nosh": "no-safe-harbour",
                              "noweb": "no-web-scope"})
        self.assertEqual(res[0]["handle"], "ok")
        res = by_handle(R.score(progs, W, NOW, include_no_bounty=True))
        self.assertIsNone(res["nobounty"]["excluded"])
        self.assertIsNotNone(res["nobounty"]["total"])

    def test_join_ordering(self):
        progs = [prog("full-low", max_eur=100, min_eur=0, rps=0),
                 prog("join-rich", max_eur=50000, min_eur=1000, detail="needs-join"),
                 prog("full-high", max_eur=5000)]
        res = R.score(progs, W, NOW)
        order = [r["handle"] for r in res]
        self.assertEqual(order[-1], "join-rich")
        j = by_handle(res)["join-rich"]
        self.assertEqual(j["flags"], ["join"])
        self.assertIsNone(j["factors"]["findability"])
        self.assertIsNone(j["factors"]["friction"])
        self.assertAlmostEqual(j["factors"]["payout"], 1.0)  # no paid_share multiplier
        mixed = [r["handle"] for r in R.score(progs, W, NOW, include_join=True)]
        self.assertEqual(mixed[0], "join-rich")
        # join programs are never dropped for safe harbour / web scope (unknown)
        self.assertIsNone(j["excluded"])
        jb = by_handle(R.score([prog("j0", max_eur=0, min_eur=0, detail="needs-join")], W, NOW))
        self.assertEqual(jb["j0"]["excluded"], "no-bounty")


class TestOutput(unittest.TestCase):
    def test_json_and_text(self):
        progs = [prog("intigriti-a"), prog("intigriti-b", detail="needs-join"),
                 prog("intigriti-c", max_eur=0, min_eur=0)]
        res = R.score(progs, W, NOW)
        data = json.loads(json.dumps(R.to_json(res)))
        self.assertEqual([d["handle"] for d in data], ["intigriti-a", "intigriti-b", "intigriti-c"])
        self.assertEqual(set(data[0]), {"handle", "total", "factors", "inputs", "flags", "excluded"})
        self.assertEqual(set(data[0]["factors"]), {"payout", "findability", "competition", "friction"})
        self.assertEqual(data[2]["excluded"], "no-bounty")
        text = R.render_table(res)
        self.assertTrue(all(len(line) <= 120 for line in text.splitlines()))
        self.assertIn("join", text)
        self.assertIn("excluded no-bounty (1):", text)
        exp = R.render_explain(res, "intigriti-b", W)
        self.assertIn("list fields only", exp)

    def test_default_config_written(self):
        ws = tempfile.mkdtemp()
        try:
            cfg = load_ranking(ws)
            self.assertEqual(cfg, DEFAULT_RANKING)
            with open(os.path.join(ws, "config", "ranking.json")) as f:
                self.assertEqual(json.load(f), DEFAULT_RANKING)
            with open(os.path.join(ws, "config", "ranking.json"), "w") as f:
                json.dump({"weights": {"payout": 1.0}}, f)
            cfg = load_ranking(ws)
            self.assertEqual(cfg["weights"]["payout"], 1.0)
            self.assertEqual(cfg["weights"]["friction"], 0.10)
        finally:
            shutil.rmtree(ws)


if __name__ == "__main__":
    unittest.main()
