"""CONTRACTS §7 findings, §14 time/€/h, `program show`, and bounty-evidence's redaction."""

import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from bounty import findings, show, timelog
from bounty.util import BountyError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
T0 = datetime.datetime(2026, 11, 19, 10, 0, tzinfo=datetime.timezone.utc)


def _evidence_module():
    sys.path.insert(0, os.path.join(ROOT, "container", "share", "bounty"))
    path = os.path.join(ROOT, "container", "bin", "bounty-evidence")
    loader = importlib.machinery.SourceFileLoader("bounty_evidence", path)
    spec = importlib.util.spec_from_loader("bounty_evidence", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _r(path):
    with open(path) as f:
        return f.read()


def _w(path, text):
    with open(path, "w") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws)
        for h, ai, plat in (("test-acme", "allowed-disclosed", "intigriti"), ("manual-friend", "unknown", "manual")):
            d = os.path.join(self.ws, "programs", h)
            os.makedirs(d)
            with open(os.path.join(d, "program.json"), "w") as f:
                json.dump({"handle": h, "name": h.title(), "platform": plat, "status": "open",
                           "legal_basis": "authorization" if plat == "manual" else "program",
                           "authorization": {"owner": "Owner", "note_path": None} if plat == "manual" else None,
                           "scope": {"in": [{"type": "wildcard", "value": "*.acme.test"}], "out": []},
                           "rules": {"rate_limit_rps": 2, "automation": "allowed", "ai_use": ai}}, f)


class FrontMatter(unittest.TestCase):
    def test_round_trip(self):
        front = {"program": "test-acme", "title": "IDOR: share link, other's file", "severity": "high",
                 "cvss": "7.5", "status": "draft", "flows": ["0f8fad5b-d9cb", "abcdef12"], "ai_used": True,
                 "created": "2026-11-19T10:00:00Z", "submitted": None, "payout_eur": 750}
        parsed, body = findings.parse(findings.dump(front) + "## Summary\n")
        self.assertEqual(parsed, front)
        self.assertEqual(body, "## Summary\n")

    def test_no_front_matter(self):
        self.assertEqual(findings.parse("# Title\n"), ({}, "# Title\n"))


class Findings(Base):
    def test_new_and_list(self):
        out = io.StringIO()
        findings.new(self.ws, "idor-share", "IDOR on share links", "test-acme", out=out)
        p = os.path.join(self.ws, "findings", "test-acme-idor-share", "finding.md")
        text = _r(p)
        front, body = findings.parse(text)
        self.assertEqual((front["program"], front["status"], front["severity"], front["flows"]),
                         ("test-acme", "draft", "tbd", []))
        for h in ("## Summary", "## Steps to reproduce", "## Impact", "## Evidence", "## Suggested fix", "## AI usage"):
            self.assertIn(h, body)
        self.assertIn("disclose it", body)
        self.assertTrue(os.path.isdir(os.path.join(self.ws, "findings", "test-acme-idor-share", "evidence")))
        with self.assertRaises(BountyError):
            findings.new(self.ws, "idor-share", None, "test-acme", out=out)       # never overwrites
        findings.new(self.ws, "x", None, "manual-friend", out=out)
        self.assertIn("goes to the owner", _r(os.path.join(self.ws, "findings", "manual-friend-x", "finding.md")))
        rs = findings.rows(self.ws, "test-acme")
        self.assertEqual([r["id"] for r in rs], ["test-acme-idor-share"])

    def test_bad_slug_and_program(self):
        for bad in ("", "Has Caps", "../x", "a" * 61):
            with self.assertRaises(BountyError):
                findings.new(self.ws, bad, None, "test-acme", out=io.StringIO())
        with self.assertRaises(BountyError):
            findings.new(self.ws, "ok", None, "no-such-program", out=io.StringIO())

    def test_legacy_draft(self):
        d = os.path.join(self.ws, "findings", "old-one")
        os.makedirs(d)
        with open(os.path.join(d, "finding.md"), "w") as f:
            f.write("# Open bucket\n\n- **Program:** test-acme (Acme)\n- **Severity:** Critical (pre-launch)\n"
                    "- **Status:** Confirmed (read)\n- **Date:** 2026-11-15\n")
        r = findings.rows(self.ws)[0]
        self.assertEqual((r["program"], r["severity"], r["status"], r["title"], r["created"]),
                         ("test-acme", "critical", "draft", "Open bucket", "2026-11-15"))

    def test_evidence_writes_files_and_flows(self):
        findings.new(self.ws, "idor", None, "test-acme", out=io.StringIO())
        items = [{"flow_id": "abcdef12", "text": "# note\nGET /x\n"}]
        with mock.patch.object(findings.guard, "running", return_value=True), \
                mock.patch.object(findings.guard, "_docker", return_value=(0, json.dumps(items), "")) as d:
            findings.evidence(self.ws, "test-acme-idor", ["abcdef12"], out=io.StringIO())
        args = d.call_args[0][0]
        self.assertEqual(args[-4:], ["--handle", "test-acme", "--ids", "abcdef12"])
        base = os.path.join(self.ws, "findings", "test-acme-idor")
        self.assertEqual(_r(os.path.join(base, "evidence", "abcdef12.txt")), "# note\nGET /x\n")
        front, body = findings.parse(_r(os.path.join(base, "finding.md")))
        self.assertEqual(front["flows"], ["abcdef12"])
        self.assertIn("## Summary", body)

    def test_evidence_refuses(self):
        findings.new(self.ws, "idor", None, "test-acme", out=io.StringIO())
        for fid, ids in (("nope", ["abcdef12"]), ("../x", ["abcdef12"]), ("test-acme-idor", ["--x"]),
                         ("test-acme-idor", [])):
            with self.assertRaises(BountyError):
                findings.evidence(self.ws, fid, ids, out=io.StringIO())
        with mock.patch.object(findings.guard, "running", return_value=False):
            with self.assertRaises(BountyError) as cm:
                findings.evidence(self.ws, "test-acme-idor", ["abcdef12"], out=io.StringIO())
        self.assertEqual(cm.exception.exit_code, 3)


class Time(Base):
    def test_start_stop_report(self):
        out = io.StringIO()
        timelog.start(self.ws, "recon", "test-acme", out=out, now=T0)
        timelog.start(self.ws, "ac", "test-acme", out=out, now=T0 + datetime.timedelta(hours=1))   # stops the first
        timelog.stop(self.ws, out=out, now=T0 + datetime.timedelta(hours=2, minutes=30))
        lines = [json.loads(l) for l in _r(os.path.join(self.ws, "programs", "test-acme", "time.jsonl")).splitlines()]
        self.assertEqual([l["note"] for l in lines], ["recon", "ac"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "time-open.json")))
        findings.new(self.ws, "paid", None, "test-acme", out=io.StringIO())
        p = os.path.join(self.ws, "findings", "test-acme-paid", "finding.md")
        front, body = findings.parse(_r(p))
        front.update(status="accepted", payout_eur=500)
        _w(p, findings.dump(front) + body)
        r = timelog.report_rows(self.ws, 30, now=T0 + datetime.timedelta(days=1))[0]
        self.assertEqual((r["handle"], r["hours"], r["sessions"], r["payout_eur"], r["eur_per_hour"], r["findings"]),
                         ("test-acme", 2.5, 2, 500.0, 200.0, {"accepted": 1}))

    def test_since_cuts_a_session(self):
        timelog.start(self.ws, None, "test-acme", out=io.StringIO(), now=T0)
        timelog.stop(self.ws, out=io.StringIO(), now=T0 + datetime.timedelta(hours=4))
        # window starts 1 h before the session ended -> only that hour counts
        r = timelog.report_rows(self.ws, 1 / 24.0, now=T0 + datetime.timedelta(hours=4))[0]
        self.assertEqual(r["hours"], 1.0)

    def test_running_timer_counts(self):
        timelog.start(self.ws, None, "test-acme", out=io.StringIO(), now=T0)
        r = timelog.report_rows(self.ws, 30, now=T0 + datetime.timedelta(minutes=90))[0]
        self.assertEqual(r["hours"], 1.5)
        out = io.StringIO()
        timelog.status(self.ws, out=out, now=T0 + datetime.timedelta(minutes=90))
        self.assertIn("running: test-acme for 1h30", out.getvalue())

    def test_needs_a_program(self):
        with mock.patch.object(findings.guard, "running", return_value=False):
            with self.assertRaises(BountyError) as cm:
                timelog.start(self.ws, out=io.StringIO())
        self.assertEqual(cm.exception.exit_code, 64)


class Show(Base):
    def test_summary(self):
        findings.new(self.ws, "a", None, "manual-friend", out=io.StringIO())
        s = show.summary(self.ws, "manual-friend")
        self.assertEqual(s["legal_basis"], "authorization")
        self.assertEqual([f["id"] for f in s["findings"]], ["manual-friend-a"])
        self.assertEqual(s["recon"], {"hosts": 0, "live": 0, "new": 0, "exposed": 0})
        out = io.StringIO()
        show.show(self.ws, "manual-friend", out=out)
        self.assertIn("written note: NOT RECORDED", out.getvalue())


class Redaction(unittest.TestCase):
    def test_headers(self):
        ev = _evidence_module()
        r = ev.redact_header
        self.assertEqual(r("Cookie", "sid=SECRET123; theme=dark"), "sid=<redacted>; theme=<redacted>")
        self.assertEqual(r("Set-Cookie", "sid=SECRET; Path=/; HttpOnly"), "sid=<redacted>; Path=/; HttpOnly")
        for h in ("Authorization", "X-Csrf-Token", "X-Api-Key", "X-Session-Id", "Proxy-Authorization"):
            self.assertEqual(r(h, "SECRET"), "<redacted>", h)
        self.assertEqual(r("Content-Type", "application/json"), "application/json")
        self.assertEqual(r("User-Agent", "Intigriti - x - y"), "Intigriti - x - y")

    def test_bodies(self):
        ev = _evidence_module()
        out = json.loads(ev.redact_body('{"email":"a@x.test","password":"hunter2","nested":{"access_token":"t","name":"n"},'
                                        '"items":[{"api_key":"k","id":7}],"code":"123456"}', "application/json"))
        self.assertEqual(out, {"email": "a@x.test", "password": "<redacted>",
                               "nested": {"access_token": "<redacted>", "name": "n"},
                               "items": [{"api_key": "<redacted>", "id": 7}], "code": "<redacted>"})
        self.assertEqual(ev.redact_body("user=bob&pass=pw&remember=1", "application/x-www-form-urlencoded"),
                         "user=bob&pass=<redacted>&remember=1")
        self.assertEqual(ev.redact_body("plain text password=x", "text/plain"), "plain text password=x")
        self.assertEqual(ev.redact_body("{not json", "application/json"), "{not json")


if __name__ == "__main__":
    unittest.main()
