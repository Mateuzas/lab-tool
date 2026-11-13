import unittest

from bounty import normalise as n
from bounty.sync import assign_handles
from bounty.tests import fixtures as fx


class TestMapping(unittest.TestCase):
    def test_types(self):
        self.assertEqual(n.map_type("Wildcard", "*.a.com"), "wildcard")
        self.assertEqual(n.map_type("IpRange", "10.0.0.0/8"), "cidr")
        self.assertEqual(n.map_type("SourceCode", "https://github.com/x/y"), "source")
        self.assertEqual(n.map_type("iOS", "123"), "ios")
        self.assertEqual(n.map_type("Android", "com.x"), "android")
        self.assertEqual(n.map_type("Device", "Router"), "device")
        self.assertEqual(n.map_type("Other", "Misc"), "other")
        self.assertEqual(n.map_type("SomethingNew", "x"), "other")

    def test_bare_host_url_becomes_domain(self):
        self.assertEqual(n.map_type("Url", "www.acme.example"), "domain")
        self.assertEqual(n.map_type("Url", " WWW.Acme.Example. "), "domain")
        self.assertEqual(n.map_type("Url", "https://www.acme.example/"), "url")
        self.assertEqual(n.map_type("Url", "www.acme.example/shop"), "url")
        self.assertEqual(n.map_type("Url", "Corporate Sites"), "url")

    def test_tiers(self):
        self.assertEqual(n.map_tier("Tier 1"), "tier-1")
        self.assertEqual(n.map_tier("Tier 5"), "tier-5")
        self.assertEqual(n.map_tier("No Bounty"), "no-bounty")
        self.assertEqual(n.map_tier("Out Of Scope"), "out")

    def test_slug_and_handle_collisions(self):
        self.assertEqual(n.slug("nestlévdp"), "nestlevdp")
        self.assertEqual(n.slug("engel&vlkers"), "engel-and-vlkers")
        self.assertEqual(n.slug("VDP"), "vdp")
        recs = [fx.list_record(pid="a", handle="vdp", company="showpad"),
                fx.list_record(pid="b", handle="VDP", company="nine altitudes"),
                fx.list_record(pid="c", handle="Acme")]
        h = assign_handles(recs, {})
        self.assertEqual(h["a"], "intigriti-showpad-vdp")
        self.assertEqual(h["b"], "intigriti-nine-altitudes-vdp")
        self.assertEqual(h["c"], "intigriti-acme")
        # existing handles are stable; a newcomer that collides gets the company form
        h2 = assign_handles(recs + [fx.list_record(pid="d", handle="acme", company="other")],
                            {"c": "intigriti-acme"})
        self.assertEqual(h2["c"], "intigriti-acme")
        self.assertEqual(h2["d"], "intigriti-other-acme")


class TestProgram(unittest.TestCase):
    def build(self, **kw):
        rec = fx.list_record(**{k: v for k, v in kw.items() if k in ("max_b", "min_b", "currency", "conf")})
        det = fx.detail(rec, **{k: v for k, v in kw.items() if k not in ("max_b", "min_b", "currency", "conf")})
        return n.program("intigriti-acme", rec, det, "ok", fx.FX, "2026-11-13T12:00:00Z",
                         "2025-01-10T08:00:00Z")

    def test_profile(self):
        p = self.build()
        self.assertEqual(p["schema"], 1)
        self.assertEqual(p["status"], "open")
        self.assertEqual(p["confidentiality"], "public")
        self.assertEqual(p["detail"], "ok")
        self.assertEqual(p["first_seen_at"], "2025-01-10T08:00:00Z")
        s = p["scope"]
        self.assertEqual(s["version"], "dom-v1")
        self.assertEqual(s["changed_at"], "2026-09-21T14:13:20Z")
        self.assertEqual([(e["type"], e["value"], e["tier"]) for e in s["in"]],
                         [("wildcard", "*.acme.example", "tier-1"),
                          ("url", "https://app.acme.example/api", "tier-2"),
                          ("domain", "www.acme.example", "no-bounty")])
        self.assertEqual(s["out"], [{"type": "domain", "value": "status.acme.example",
                                     "description": "third party"}])
        self.assertEqual(s["in"][0]["description"], "")

    def test_rules(self):
        r = self.build()["rules"]
        self.assertEqual(r["request_header"], "X-Intigriti-Username: {Username}")
        self.assertEqual(r["rate_limit_rps"], 5)
        self.assertEqual(r["automation"], "allowed")
        self.assertTrue(r["safe_harbour"])
        self.assertTrue(r["intigriti_me"])
        self.assertIsNone(r["user_agent"])
        self.assertEqual(r["ai_use"], "allowed-disclosed")
        self.assertEqual(self.build(rps=0)["rules"]["automation"], "forbidden")
        self.assertEqual(self.build(rps=None)["rules"]["automation"], "unknown")
        self.assertIsNone(self.build(request_header="  ")["rules"]["request_header"])
        self.assertEqual(self.build(user_agent="Intigriti-{Username}")["rules"]["user_agent"],
                         "Intigriti-{Username}")

    def test_currency_to_eur(self):
        b = self.build(min_b=100, max_b=1000, currency="USD")["bounty"]
        self.assertEqual((b["min"], b["max"], b["currency"]), (100, 1000, "USD"))
        self.assertEqual((b["min_eur"], b["max_eur"]), (92, 920))
        b = self.build(max_b=1000, currency="GBP")["bounty"]
        self.assertEqual(b["max_eur"], 1170)
        b = self.build(max_b=1000, currency="CHF")["bounty"]
        self.assertIsNone(b["max_eur"])

    def test_needs_join_and_carry_over(self):
        rec = fx.list_record()
        p = n.program("intigriti-acme", rec, None, "needs-join", fx.FX, "t", None)
        self.assertEqual(p["detail"], "needs-join")
        self.assertIsNone(p["scope"])
        self.assertIsNone(p["rules"])
        full = n.program("intigriti-acme", rec, fx.detail(rec), "ok", fx.FX, "t", None)
        full["rules"]["ai_use"] = "forbidden"
        full["legal_basis"] = "lt-article-25"
        susp = fx.list_record(status="Suspended")
        q = n.program("intigriti-acme", susp, None, "not-fetched", fx.FX, "t", None, full)
        self.assertEqual(q["status"], "suspended")
        self.assertEqual(q["detail"], "ok")
        self.assertEqual(q["scope"], full["scope"])
        again = n.program("intigriti-acme", rec, fx.detail(rec), "ok", fx.FX, "t", None, full)
        self.assertEqual(again["rules"]["ai_use"], "forbidden")
        self.assertEqual(again["legal_basis"], "lt-article-25")


if __name__ == "__main__":
    unittest.main()
