"""Unit tests for the scope guard decision logic (container/share/bounty/guard_policy.py).

Run on the Mac (python3 3.9+) and in the image:
  python3 -B -m unittest discover -s tests -p 'test_guard_*.py'
No mitmproxy, no network.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "container", "share", "bounty"))
import guard_policy as gp  # noqa: E402


def profile(ins=None, outs=None, **rules):
    r = {"rate_limit_rps": 5, "automation": "allowed", "user_agent": "Intigriti - <username> - <user agent>",
         "request_header": "X-Intigriti-Username: {Username}", "ai_use": "allowed-disclosed"}
    r.update(rules)
    return {
        "schema": 1, "handle": "intigriti-acme", "detail": "ok",
        "scope": {
            "in": ins if ins is not None else [
                {"type": "wildcard", "value": "*.acme.com"},
                {"type": "domain", "value": "acme.com"},
                {"type": "url", "value": "acme.com/paper"},
                {"type": "url", "value": "https://docs.example.org/api/"},
                {"type": "domain", "value": "Shop.Example.NET."},
                {"type": "cidr", "value": "198.51.100.0/24"},
                {"type": "android", "value": "com.acme.app"},
                {"type": "other", "value": "dash.example.io"},
            ],
            "out": outs if outs is not None else [
                {"type": "domain", "value": "community.acme.com"},
                {"type": "wildcard", "value": "*.legacy.acme.com"},
                {"type": "url", "value": "https://app.acme.com/admin"},
                {"type": "other", "value": "status.acme.com"},
                {"type": "other", "value": "Acme Passwords"},
                {"type": "cidr", "value": "198.51.100.128/25"},
            ],
        },
        "rules": r,
    }


RECON = {"hosts": ["crt.sh", "web.archive.org"], "rate_limit_rps": 1}


def pol(p=None, username="tester", recon=RECON):
    return gp.Policy(p or profile(), username, recon, "intigriti-acme")


def decide(policy, host, path="/", actor="human", scheme="https", port=443, sni=None, host_header=None,
           authority=None, ips=None):
    return gp.decide(policy, authority or host, sni if sni is not None else host,
                     host_header if host_header is not None else host, scheme == "https", scheme, port, path,
                     actor, ips)


class Browse(unittest.TestCase):
    """CONTRACTS §3.5: approved browse-only hosts."""

    def setUp(self):
        p = profile()
        p["browse"] = {"hosts": {"cdn.acmestatic.com": {"methods": "read"}, "login.idp.test": {"methods": "any"},
                                 "community.acme.com": {"methods": "any"},     # out of scope: never usable
                                 "10.0.0.1": {}, "*.wild.test": {}, "bad host": {}}}
        self.p = pol(p)

    def d(self, host, actor="human", method="GET", ips=None):
        return gp.decide(self.p, host, host, host, True, "https", 443, "/x", actor, ips, method=method)

    def test_human_read_allowed_as_browse(self):
        r = self.d("cdn.acmestatic.com")
        self.assertEqual((r.allowed, r.category), (True, "browse"))
        self.assertTrue(self.d("cdn.acmestatic.com", method="HEAD").allowed)

    def test_tools_and_claude_refused(self):
        self.assertEqual(self.d("cdn.acmestatic.com", actor="tool:katana").reason, "browse-human-only")
        self.assertEqual(self.d("cdn.acmestatic.com", actor="claude").reason, "browse-human-only")

    def test_write_methods_need_any(self):
        self.assertEqual(self.d("cdn.acmestatic.com", method="POST").reason, "browse-read-only")
        self.assertEqual(self.d("cdn.acmestatic.com", method=None).reason, "browse-read-only")
        self.assertTrue(self.d("login.idp.test", method="POST").allowed)

    def test_out_of_scope_never_browse(self):
        self.assertEqual(self.d("community.acme.com").reason, "out-of-scope")

    def test_internal_address_still_refused(self):
        self.assertEqual(self.d("cdn.acmestatic.com", ips=["10.1.2.3"]).reason, "internal-address")

    def test_malformed_entries_dropped(self):
        self.assertEqual(sorted(self.p.browse), ["cdn.acmestatic.com", "community.acme.com", "login.idp.test"])
        self.assertEqual(self.d("other.test").reason, "not-in-scope")

    def test_host_level_allows_upstream_connect(self):
        self.assertEqual(self.p.host_decision("cdn.acmestatic.com"), (None, "browse"))
        self.assertEqual(self.p.host_decision("community.acme.com")[0], "out-of-scope")

    def test_no_browse_key_is_the_old_behaviour(self):
        p = pol()
        self.assertEqual(p.browse, {})
        self.assertEqual(gp.decide(p, "cdn.acmestatic.com", "cdn.acmestatic.com", "cdn.acmestatic.com", True,
                                   "https", 443, "/", "human", method="GET").reason, "not-in-scope")

    def test_in_scope_targets_unchanged(self):
        r = self.d("www.acme.com", actor="tool:katana", method="POST")
        self.assertEqual((r.allowed, r.category), (True, "target"))


class ScopeMatching(unittest.TestCase):
    def setUp(self):
        self.p = pol()

    def reason(self, *a, **k):
        return decide(self.p, *a, **k).reason

    def test_wildcard_any_depth_not_apex(self):
        self.assertIsNone(self.reason("www.acme.com"))
        self.assertIsNone(self.reason("a.b.c.acme.com"))
        p = pol(profile(ins=[{"type": "wildcard", "value": "*.acme.com"}]))
        self.assertEqual(decide(p, "acme.com").reason, "not-in-scope")
        self.assertEqual(decide(p, "evilacme.com").reason, "not-in-scope")
        self.assertEqual(decide(p, "acme.com.evil.net").reason, "not-in-scope")

    def test_domain_exact_and_normalised(self):
        self.assertIsNone(self.reason("acme.com"))
        self.assertIsNone(self.reason("shop.example.net"))
        self.assertIsNone(self.reason("SHOP.example.net."))
        self.assertEqual(self.reason("www.shop.example.net"), "not-in-scope")

    def test_out_beats_in(self):
        self.assertEqual(self.reason("community.acme.com"), "out-of-scope")
        self.assertEqual(self.reason("x.legacy.acme.com"), "out-of-scope")
        self.assertEqual(self.reason("legacy.acme.com"), "out-of-scope")   # out wildcard covers its apex

    def test_out_url_path_prefix(self):
        self.assertEqual(self.reason("app.acme.com", "/admin"), "out-of-scope")
        self.assertEqual(self.reason("app.acme.com", "/admin/users?x=1"), "out-of-scope")
        self.assertEqual(self.reason("app.acme.com", "/ADMIN/"), "out-of-scope")
        self.assertEqual(self.reason("app.acme.com", "/x/..%2fadmin"), "out-of-scope")
        self.assertEqual(self.reason("app.acme.com", "/x/%252e%252e/admin"), "out-of-scope")
        self.assertEqual(self.reason("app.acme.com", "//admin"), "out-of-scope")
        self.assertIsNone(self.reason("app.acme.com", "/administrator"))
        self.assertIsNone(self.reason("app.acme.com", "/"))

    def test_out_other_hostname_enforced(self):
        self.assertEqual(self.reason("status.acme.com"), "out-of-scope")

    def test_in_url_without_scheme(self):
        p = pol(profile(ins=[{"type": "url", "value": "acme.com/paper"}]))
        self.assertIsNone(decide(p, "acme.com", "/paper").reason)
        self.assertIsNone(decide(p, "acme.com", "/paper/doc?x=1").reason)
        self.assertIsNone(decide(p, "acme.com", "/paper", scheme="http", port=80).reason)
        self.assertEqual(decide(p, "acme.com", "/paperclip").reason, "not-in-scope")
        self.assertEqual(decide(p, "acme.com", "/").reason, "not-in-scope")
        self.assertEqual(decide(p, "acme.com", "/paper/../account").reason, "not-in-scope")
        self.assertEqual(decide(p, "acme.com", "/paper/%2e%2e/account").reason, "not-in-scope")
        self.assertEqual(decide(p, "acme.com", "/paper/%252e%252e/account").reason, "not-in-scope")
        self.assertEqual(p.host_decision("acme.com"), (None, "target"))   # CONNECT level: host may be used

    def test_in_url_with_scheme(self):
        p = pol(profile(ins=[{"type": "url", "value": "https://docs.example.org/api/"}]))
        self.assertIsNone(decide(p, "docs.example.org", "/api/v1").reason)
        self.assertIsNone(decide(p, "docs.example.org", "/api").reason)
        self.assertEqual(decide(p, "docs.example.org", "/api/v1", scheme="http", port=80).reason, "not-in-scope")

    def test_cidr_matches_ip_literals_only(self):
        self.assertIsNone(self.reason("198.51.100.7"))
        self.assertEqual(self.reason("198.51.100.200"), "out-of-scope")
        self.assertEqual(self.reason("203.0.113.1"), "not-in-scope")
        self.assertEqual(self.address("www.acme.com", ["198.51.100.200"]), "out-of-scope")

    def test_unenforceable_types_ignored(self):
        self.assertEqual(self.reason("dash.example.io"), "not-in-scope")   # "other" in scope.in

    def test_recon_source(self):
        d = decide(self.p, "crt.sh", "/?q=%25.acme.com", actor="claude")
        self.assertTrue(d.allowed)
        self.assertEqual(d.category, "recon-source")
        self.assertEqual(self.reason("sub.crt.sh"), "not-in-scope")

    def test_out_beats_recon(self):
        p = pol(profile(outs=[{"type": "domain", "value": "crt.sh"}]))
        self.assertEqual(decide(p, "crt.sh").reason, "out-of-scope")

    def address(self, host, ips):
        return decide(self.p, host, ips=ips).reason

    def test_internal_addresses(self):
        for ip in ("10.1.2.3", "172.16.0.1", "192.168.65.7", "127.0.0.1", "169.254.169.254", "100.64.0.1",
                   "0.0.0.0", "::1", "fe80::1", "fd00::1", "::ffff:10.0.0.1", "64:ff9b::a00:1", "224.0.0.1"):
            self.assertEqual(self.address("www.acme.com", ["93.184.216.34", ip]), "internal-address", ip)
        self.assertIsNone(self.address("www.acme.com", ["93.184.216.34", "2606:2800:220:1::1"]))

    def test_internal_listed(self):
        p = pol(profile(ins=[{"type": "wildcard", "value": "*.acme.com"}, {"type": "cidr", "value": "10.9.0.0/16"}]))
        self.assertIsNone(decide(p, "vpn.acme.com", ips=["10.9.1.1"]).reason)
        self.assertEqual(decide(p, "vpn.acme.com", ips=["10.8.1.1"]).reason, "internal-address")
        self.assertIsNone(decide(p, "10.9.3.4", ips=["10.9.3.4"]).reason)
        self.assertEqual([str(n) for n in p.listed_networks], ["10.9.0.0/16"])


class HostAgreement(unittest.TestCase):
    def setUp(self):
        self.p = pol()

    def test_all_three_agree(self):
        self.assertTrue(decide(self.p, "www.acme.com", sni="WWW.acme.com.", host_header="www.acme.com:443").allowed)

    def test_mismatch(self):
        self.assertEqual(decide(self.p, "www.acme.com", host_header="community.acme.com").reason, "host-mismatch")
        self.assertEqual(decide(self.p, "www.acme.com", sni="evil.com").reason, "host-mismatch")
        self.assertEqual(decide(self.p, "www.acme.com", authority="evil.com").reason, "host-mismatch")
        self.assertEqual(decide(self.p, "www.acme.com", host_header="").reason, "host-mismatch")

    def test_mismatch_comes_before_no_profile(self):
        self.assertEqual(decide(None, "a.com", host_header="b.com").reason, "host-mismatch")
        self.assertEqual(decide(None, "a.com").reason, "no-profile")

    def test_tls_without_sni(self):
        self.assertEqual(decide(self.p, "www.acme.com", sni="").reason, "host-mismatch")
        self.assertTrue(decide(self.p, "198.51.100.7", sni="").allowed)

    def test_plain_http_has_no_sni(self):
        d = gp.decide(self.p, "www.acme.com", None, "www.acme.com", False, "http", 80, "/", "human")
        self.assertTrue(d.allowed)


class Actors(unittest.TestCase):
    def test_actor_resolution(self):
        self.assertEqual(gp.actor_for("human", None), "human")
        self.assertEqual(gp.actor_for("human", "tool:ffuf"), "tool:ffuf")
        self.assertEqual(gp.actor_for("human", "Claude"), "claude")
        self.assertEqual(gp.actor_for("human", "root; rm -rf"), "tool:unknown")
        self.assertEqual(gp.actor_for("claude", "human"), "claude")
        self.assertEqual(gp.actor_for("claude", None), "claude")

    def test_rps_zero(self):
        p = pol(profile(rate_limit_rps=0, automation="forbidden"))
        self.assertFalse(p.automation)
        self.assertEqual(p.rps, gp.DEFAULT_RPS)
        self.assertTrue(decide(p, "www.acme.com", actor="human").allowed)
        self.assertEqual(decide(p, "www.acme.com", actor="claude").reason, "automation-forbidden")
        self.assertEqual(decide(p, "www.acme.com", actor="tool:nuclei").reason, "automation-forbidden")
        self.assertTrue(decide(p, "crt.sh", actor="tool:subfinder").allowed)   # recon sources are not the target

    def test_rps_null_defaults_to_2(self):
        p = pol(profile(rate_limit_rps=None, automation="unknown"))
        self.assertEqual(p.rps, 2.0)
        self.assertTrue(p.automation)

    def test_ai_forbidden(self):
        p = pol(profile(ai_use="forbidden"))
        self.assertEqual(decide(p, "www.acme.com", actor="claude").reason, "automation-forbidden")
        self.assertTrue(decide(p, "www.acme.com", actor="tool:ffuf").allowed)


class Templates(unittest.TestCase):
    def test_dropbox_style(self):
        p = pol()
        h = p.ident.headers("Mozilla/5.0 (X11)")
        self.assertEqual(h["X-Intigriti-Username"], "tester")
        self.assertEqual(h["User-Agent"], "Intigriti - tester - Mozilla/5.0 (X11)")
        self.assertEqual(p.ident.headers(None)["User-Agent"], "Intigriti - tester - kali-lab")

    def test_both_spellings_case_insensitive(self):
        p = pol(profile(user_agent="{UserAgent} via <USERNAME>", request_header="X-Bug-Bounty: <Username>-{username}"))
        h = p.ident.headers("curl/8")
        self.assertEqual(h["User-Agent"], "curl/8 via tester")
        self.assertEqual(h["X-Bug-Bounty"], "tester-tester")

    def test_unknown_placeholder_exit_4(self):
        for rh in ("X-Intigriti-Username: {Handle}", "X-H: <email>"):
            with self.assertRaises(gp.PolicyError) as cm:
                pol(profile(request_header=rh))
            self.assertEqual(cm.exception.exit_code, 4)
        with self.assertRaises(gp.PolicyError) as cm:
            pol(profile(user_agent="Intigriti - {researcher}"))
        self.assertEqual(cm.exception.exit_code, 4)

    def test_malformed_header_template(self):
        for rh in ("no colon here", ": value", "Bad Name: x", "Host: evil.com", "X-Bounty-Actor: human"):
            with self.assertRaises(gp.PolicyError):
                pol(profile(request_header=rh))

    def test_username_required_and_safe(self):
        with self.assertRaises(gp.PolicyError) as cm:
            pol(username=None)
        self.assertEqual(cm.exception.exit_code, 2)
        with self.assertRaises(gp.PolicyError) as cm:
            pol(username="bob\r\nX-Evil: 1")
        self.assertEqual(cm.exception.exit_code, 2)
        p = pol(profile(user_agent=None, request_header="X-Bug-Bounty: true"), username=None)
        self.assertEqual(p.ident.headers("x"), {"X-Bug-Bounty": "true"})

    def test_original_ua_with_newline_dropped(self):
        self.assertEqual(pol().ident.headers("a\r\nX: y")["User-Agent"], "Intigriti - tester - kali-lab")


class ProfileValidation(unittest.TestCase):
    def test_no_detail(self):
        p = profile()
        p["scope"] = None
        with self.assertRaises(gp.PolicyError):
            pol(p)

    def test_bad_entries_refuse_whole_profile(self):
        for bad in ({"type": "cidr", "value": "10.0.0.0/33"}, {"type": "domain", "value": "exa mple.com"},
                    {"type": "url", "value": 5}, "nope"):
            p = profile()
            p["scope"]["in"].append(bad)
            with self.assertRaises(gp.PolicyError):
                pol(p)

    def test_bad_rate(self):
        for v in (-1, "5", True):
            with self.assertRaises(gp.PolicyError):
                pol(profile(rate_limit_rps=v))

    def test_handle_must_match(self):
        with self.assertRaises(gp.PolicyError):
            gp.Policy(profile(), "tester", RECON, "intigriti-other")

    def test_no_enforceable_in_scope(self):
        with self.assertRaises(gp.PolicyError):
            pol(profile(ins=[{"type": "android", "value": "com.acme"}]))

    def test_odd_in_wildcard_allows_nothing(self):
        p = pol(profile(ins=[{"type": "wildcard", "value": "*acme*"}, {"type": "domain", "value": "acme.com"}]))
        self.assertEqual(decide(p, "xacmex.com").reason, "not-in-scope")

    def test_odd_out_wildcard_is_a_glob(self):
        p = pol(profile(outs=[{"type": "wildcard", "value": "*.acme.*"}]))
        self.assertEqual(decide(p, "www.acme.com").reason, "out-of-scope")

    def test_profile_not_mutated(self):
        p = profile()
        before = copy.deepcopy(p)
        pol(p)
        self.assertEqual(p, before)

    def test_recon_config(self):
        with self.assertRaises(gp.PolicyError):
            gp.compile_recon({"hosts": ["*.crt.sh"]})
        with self.assertRaises(gp.PolicyError):
            gp.compile_recon({"hosts": ["crt.sh"], "rate_limit_rps": 0})
        with self.assertRaises(gp.PolicyError):
            gp.compile_recon({"hosts": ["crt.sh"], "egress_ip_url": "https://api.ipify.org"})
        self.assertEqual(gp.compile_recon(None)["rate_limit_rps"], 2.0)


class RateLimit(unittest.TestCase):
    def test_spacing_and_refusal(self):
        rl = gp.RateLimiter(2)
        waits = [rl.reserve(100.0) for _ in range(25)]
        self.assertEqual(waits[:3], [0.0, 0.5, 1.0])
        allowed = [w for w in waits if w is not None]
        self.assertEqual(len(allowed), 21)       # slots 0 .. 10 s
        self.assertEqual(max(allowed), 10.0)
        self.assertTrue(all(w is None for w in waits[21:]))

    def test_refill(self):
        rl = gp.RateLimiter(5)
        self.assertEqual(rl.reserve(0.0), 0.0)
        self.assertAlmostEqual(rl.reserve(0.0), 0.2)
        self.assertEqual(rl.reserve(10.0), 0.0)


class Refusals(unittest.TestCase):
    def test_every_reason_has_text(self):
        for r in gp.REASONS:
            body = gp.refusal_body(r)
            self.assertTrue(body.startswith("bounty-guard refused: ") and body.endswith("\n"))
            self.assertNotIn(r + ")", body)


if __name__ == "__main__":
    unittest.main()
