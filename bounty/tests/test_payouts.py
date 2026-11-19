"""BE-26 (CONTRACTS §14.1): payouts sync, totals in time report, time status JSON."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from bounty import payouts, timelog
from bounty.util import iso

NOW = datetime.datetime(2026, 11, 19, 12, 0, tzinfo=datetime.timezone.utc)
DAY = 86400


def unix(dt):
    return int(dt.timestamp())


class FakeClient(object):
    def __init__(self, records, page=2):
        self.records, self.page, self.calls = records, page, []

    def get_json(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        off = params["offset"]
        return None, {"maxCount": len(self.records), "records": self.records[off:off + self.page]}


def rec(i, value, cur, paid_days_ago=None):
    return {"id": "p%d" % i, "amount": {"value": value, "currency": cur}, "status": {"id": 1, "value": "Paid" if paid_days_ago is not None else "Pending"},
            "createdAt": unix(NOW) - 40 * DAY,
            "paidAt": unix(NOW) - paid_days_ago * DAY if paid_days_ago is not None else None}


class Payouts(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws)

    def test_sync_pages_converts_and_writes(self):
        c = FakeClient([rec(1, 500, "EUR", 3), rec(2, 1000, "USD", 10), rec(3, 200, "EUR", None), rec(4, 50, "XYZ", 1)])
        out = io.StringIO()
        payouts.sync(c, self.ws, out=out, now=NOW)
        self.assertEqual([p["offset"] for _, p in c.calls], [0, 2])
        d = json.load(open(payouts.path(self.ws)))
        by = {r["id"]: r for r in d["records"]}
        self.assertEqual(by["p1"]["eur"], 500)
        self.assertEqual(by["p2"]["eur"], 920.0)            # default fx USD 0.92
        self.assertIsNone(by["p4"]["eur"])                   # unknown currency: never guessed
        self.assertIsNone(by["p3"]["paid_at"])
        self.assertIn("3 paid", out.getvalue())
        self.assertIn("1 in an unknown currency", out.getvalue())

    def test_paid_in_window(self):
        self.assertEqual(payouts.paid_in_window(self.ws, NOW), (None, None))   # never synced
        payouts.sync(FakeClient([rec(1, 500, "EUR", 3), rec(2, 1000, "USD", 40), rec(3, 200, "EUR", None)]),
                     self.ws, out=io.StringIO(), now=NOW)
        self.assertEqual(payouts.paid_in_window(self.ws, NOW - datetime.timedelta(days=30)), (1, 500.0))

    def test_report_totals_prefer_api_payouts(self):
        d = os.path.join(self.ws, "programs", "test-acme")
        os.makedirs(d)
        with open(os.path.join(d, "program.json"), "w") as f:
            json.dump({"handle": "test-acme", "name": "Acme"}, f)
        with open(os.path.join(d, "time.jsonl"), "w") as f:
            f.write(json.dumps({"start": iso(NOW - datetime.timedelta(hours=5)), "end": iso(NOW - datetime.timedelta(hours=1)), "note": ""}) + "\n")
        t = timelog.report_data(self.ws, 30, now=NOW)["totals"]
        self.assertEqual((t["hours"], t["paid_eur_api"], t["payouts_api"], t["eur_per_hour"]), (4.0, None, None, 0.0))
        payouts.sync(FakeClient([rec(1, 500, "EUR", 3)]), self.ws, out=io.StringIO(), now=NOW)
        t = timelog.report_data(self.ws, 30, now=NOW)["totals"]
        self.assertEqual((t["paid_eur_api"], t["payouts_api"], t["eur_per_hour"]), (500.0, 1, 125.0))
        out = io.StringIO()
        timelog.report(self.ws, 30, out=out, now=NOW)
        self.assertIn("€500 paid by Intigriti (1 payouts)", out.getvalue())

    def test_status_json(self):
        d = os.path.join(self.ws, "programs", "test-acme")
        os.makedirs(d)
        with open(os.path.join(d, "program.json"), "w") as f:
            json.dump({"handle": "test-acme"}, f)
        s = timelog.status_data(self.ws, now=NOW)
        self.assertEqual(s, {"running": None, "today_seconds": 0})
        timelog.start(self.ws, "x", "test-acme", out=io.StringIO(), now=NOW - datetime.timedelta(minutes=90))
        s = timelog.status_data(self.ws, now=NOW)
        self.assertEqual((s["running"]["handle"], s["running"]["seconds"], s["today_seconds"]), ("test-acme", 5400, 5400))


if __name__ == "__main__":
    unittest.main()
