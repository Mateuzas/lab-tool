"""Your own Intigriti payouts (CONTRACTS §14.1): `payouts sync` -> workspace/bounty/payouts.json.

The API's payout record has no program or submission link (`id, amount{value,currency}, status,
createdAt, paidAt`), so payouts give overall earnings and overall €/h; per-program €/h still comes
from the findings' `payout_eur`. Amounts are converted with the ranking config's fixed `fx` rates
(an unknown currency stays `eur: null`, never guessed).
"""

import os
import sys

from bounty import normalise
from bounty.config import load_ranking
from bounty.util import BountyError, iso, iso_from_unix, read_json, utcnow, write_json

PAGE = 100
FILE = "payouts.json"


def path(ws):
    return os.path.join(ws, FILE)


def _ts(v):
    try:
        return iso_from_unix(v) if isinstance(v, (int, float)) and v > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def normalise_record(r, fx):
    amt = r.get("amount") or {}
    value, cur = amt.get("value"), (amt.get("currency") or "").upper() or None
    status = r.get("status") or {}
    return {"id": r.get("id"), "amount": value, "currency": cur,
            "eur": normalise.to_eur(value, cur, fx) if value is not None else None,
            "status": status.get("value") if isinstance(status, dict) else status,
            "created_at": _ts(r.get("createdAt")), "paid_at": _ts(r.get("paidAt"))}


def fetch(client):
    out, offset = [], 0
    while True:
        _, body = client.get_json("/v1/payouts", {"limit": PAGE, "offset": offset})
        if not isinstance(body, dict) or not isinstance(body.get("records"), list):
            raise BountyError("parse", "/v1/payouts: expected {maxCount, records[]}", 4)
        recs = body["records"]
        out.extend(recs)
        offset += len(recs)
        total = body.get("maxCount")
        if not recs or total is None or offset >= int(total):
            return out


def sync(client, ws, out=sys.stdout, now=None):
    fx = load_ranking(ws)["fx"]
    recs = [normalise_record(r, fx) for r in fetch(client) if isinstance(r, dict) and r.get("id")]
    recs.sort(key=lambda r: r["paid_at"] or r["created_at"] or "", reverse=True)
    write_json(path(ws), {"synced_at": iso(now or utcnow()), "records": recs})
    paid = [r for r in recs if r["paid_at"]]
    eur = sum(r["eur"] for r in paid if r["eur"] is not None)
    unknown = sum(1 for r in paid if r["eur"] is None)
    out.write("payouts: %d (%d paid, €%.2f%s)\n" % (len(recs), len(paid), eur,
              (", %d in an unknown currency" % unknown) if unknown else ""))
    return 0


def load(ws):
    """payouts.json or None when never synced."""
    d = read_json(path(ws))
    return d if isinstance(d, dict) and isinstance(d.get("records"), list) else None


def paid_in_window(ws, since):
    """(count, eur) of payouts paid at or after `since` (datetime), or (None, None) if never synced."""
    d = load(ws)
    if d is None:
        return None, None
    since_iso = iso(since)
    rs = [r for r in d["records"] if r.get("paid_at") and r["paid_at"] >= since_iso]
    return len(rs), round(sum(r["eur"] for r in rs if isinstance(r.get("eur"), (int, float))), 2)
