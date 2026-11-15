import importlib.util
import os
import unittest

from bounty import manual


def _policy():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = os.path.join(root, "container", "share", "bounty", "guard_policy.py")
    spec = importlib.util.spec_from_file_location("guard_policy", p)
    gp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gp)
    return gp


class TestClassify(unittest.TestCase):
    def test_types(self):
        self.assertEqual(manual.classify("*.example.com")["type"], "wildcard")
        self.assertEqual(manual.classify("example.com")["type"], "domain")
        self.assertEqual(manual.classify("https://a.example.com/app")["type"], "url")
        self.assertEqual(manual.classify("a.example.com/app")["type"], "url")
        self.assertEqual(manual.classify("10.0.0.0/24")["type"], "cidr")
        self.assertEqual(manual.classify("10.0.0.5")["value"], "10.0.0.5/32")
        self.assertEqual(manual.classify("EXAMPLE.com")["value"], "example.com")

    def test_empty(self):
        with self.assertRaises(Exception):
            manual.classify("  ")


class TestProfile(unittest.TestCase):
    def test_enforceable_by_the_guard(self):
        gp = _policy()
        prof = manual.build_profile("Friend Site", ["*.friend.test", "https://app.friend.test/x"],
                                    ["beta.friend.test"], 3.0, "X-Auth: me", "Jane", None, None)
        self.assertEqual(prof["handle"], "manual-friend-site")
        self.assertEqual(prof["platform"], "manual")
        self.assertEqual(prof["legal_basis"], "authorization")
        pol = gp.Policy(prof, "me", None, prof["handle"])  # must not raise
        self.assertEqual(pol.rps, 3.0)
        # in scope allowed, out-of-scope host refused, unrelated refused
        self.assertEqual(pol.host_decision("x.friend.test")[0], None)
        self.assertEqual(pol.host_decision("beta.friend.test")[0], "out-of-scope")
        self.assertEqual(pol.host_decision("evil.test")[0], "not-in-scope")

    def test_needs_scope(self):
        with self.assertRaises(Exception):
            manual.build_profile("x", [], [], 5.0, None, None, None, None)

    def test_rate_zero_forbids_automation(self):
        prof = manual.build_profile("x", ["*.x.test"], [], 0, None, None, None, None)
        self.assertEqual(prof["rules"]["automation"], "forbidden")


if __name__ == "__main__":
    unittest.main()
