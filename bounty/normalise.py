"""API records -> program.json (CONTRACTS §2). Pure functions, no I/O."""

import re
import unicodedata

from bounty.util import iso_from_unix

TYPE_MAP = {
    "wildcard": "wildcard",
    "url": "url",
    "iprange": "cidr",
    "android": "android",
    "ios": "ios",
    "sourcecode": "source",
    "device": "device",
    "other": "other",
}
CONF_MAP = {"public": "public", "registered": "registered", "application": "application"}
PAID_TIERS = ("tier-1", "tier-2", "tier-3", "tier-4", "tier-5")
_BARE_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def _value(obj):
    """{"id":3,"value":"Open"} -> "Open"; plain strings pass through."""
    if isinstance(obj, dict):
        return obj.get("value")
    return obj


def slug(text):
    """Lower case [a-z0-9-]: accents folded, everything else becomes '-'."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("&", "-and-")
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-") or "program"


def company_from_weblink(url):
    """https://app.intigriti.com/...redirect=/programs/<company>/<handle>/detail -> <company>."""
    m = re.search(r"/programs/([^/]+)/[^/]+/detail", url or "")
    if not m:
        return None
    from urllib.parse import unquote
    return unquote(m.group(1))


def map_type(api_type, endpoint):
    t = TYPE_MAP.get(re.sub(r"[^a-z]", "", (api_type or "").lower()), "other")
    if t == "url" and _BARE_HOST.match((endpoint or "").strip().rstrip(".").lower()):
        return "domain"
    return t


def map_tier(api_tier):
    """'Tier 1' -> tier-1, 'No Bounty' -> no-bounty, 'Out Of Scope' -> out."""
    v = (api_tier or "").strip().lower()
    m = re.match(r"^tier\s*([1-5])$", v)
    if m:
        return "tier-" + m.group(1)
    if v == "out of scope":
        return "out"
    if v == "no bounty":
        return "no-bounty"
    return slug(v) if v else "unknown"


def _num(x):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def to_eur(amount, currency, fx):
    """Fixed-rate conversion from config fx. Unknown currency -> None (a missing input, never guessed)."""
    if amount is None or not currency:
        return None
    rate = fx.get(currency.upper())
    if rate is None:
        return None
    return _num(round(float(amount) * float(rate), 2))


def bounty(record, fx):
    mn = record.get("minBounty") or {}
    mx = record.get("maxBounty") or {}
    vmin, vmax = _num(mn.get("value")), _num(mx.get("value"))
    cur_max = (mx.get("currency") or mn.get("currency") or "").upper() or None
    cur_min = (mn.get("currency") or cur_max or "").upper() or None
    return {
        "min": vmin,
        "max": vmax,
        "currency": cur_max,
        "min_eur": to_eur(vmin, cur_min, fx),
        "max_eur": to_eur(vmax, cur_max, fx),
    }


def _text(x):
    """Optional template/text: stripped; empty -> None. Placeholders such as {Username} are kept verbatim."""
    if x is None:
        return None
    x = str(x).strip()
    return x or None


def scope(domains):
    if not isinstance(domains, dict):
        return None
    ins, outs = [], []
    for e in domains.get("content") or []:
        endpoint = (e.get("endpoint") or "").strip()
        t = map_type(_value(e.get("type")), endpoint)
        value = endpoint.rstrip(".").lower() if t == "domain" else endpoint
        desc = e.get("description") or ""
        tier = map_tier(_value(e.get("tier")))
        if tier == "out":
            outs.append({"type": t, "value": value, "description": desc})
        else:
            ins.append({"type": t, "value": value, "tier": tier, "description": desc})
    return {
        "version": domains.get("id"),
        "changed_at": iso_from_unix(domains.get("createdAt")),
        "in": ins,
        "out": outs,
    }


def rules(roe, prev_rules=None):
    if not isinstance(roe, dict):
        return None
    content = roe.get("content") or {}
    req = content.get("testingRequirements") or {}
    rps = req.get("automatedTooling")
    rps = _num(rps) if rps is not None else None
    if rps is None:
        automation = "unknown"
    elif rps > 0:
        automation = "allowed"
    else:
        automation = "forbidden"
    sh = content.get("safeHarbour")
    ai_use = "allowed-disclosed"
    if prev_rules and prev_rules.get("ai_use"):
        ai_use = prev_rules["ai_use"]  # a manual tag survives re-syncs
    return {
        "version": roe.get("id"),
        "changed_at": iso_from_unix(roe.get("createdAt")),
        "safe_harbour": bool(sh) if sh is not None else None,
        "rate_limit_rps": rps,
        "automation": automation,
        "intigriti_me": bool(req.get("intigritiMe")),
        "user_agent": _text(req.get("userAgent")),
        "request_header": _text(req.get("requestHeader")),
        "ai_use": ai_use,
        "text": content.get("description") or "",
    }


def program(handle, record, detail, detail_state, fx, synced_at, first_seen_at, prev=None):
    """Build program.json.

    record: the list record; detail: the detail body (dict) when detail_state == "ok".
    detail_state: ok | needs-join | error | not-fetched (programs that aren't open: no detail request;
    the previous scope/rules are carried over if there are any).
    """
    prev = prev or {}
    src = detail if (detail_state == "ok" and isinstance(detail, dict)) else {}
    status = (_value(record.get("status")) or "").strip().lower() or None
    conf = (_value(record.get("confidentialityLevel")) or "").strip().lower()
    out = {
        "schema": 1,
        "handle": handle,
        "platform": "intigriti",
        "platform_id": record.get("id"),
        "name": record.get("name"),
        "url": (record.get("webLinks") or {}).get("detail"),
        "status": status,
        "confidentiality": CONF_MAP.get(conf, conf or None),
        "following": bool(record.get("following")),
        "industry": record.get("industry") or src.get("industry"),
        "detail": detail_state,
        "synced_at": synced_at,
        "first_seen_at": first_seen_at,
        "bounty": bounty(record, fx),
        "scope": None,
        "rules": None,
        "legal_basis": prev.get("legal_basis") or "program",
    }
    if detail_state == "ok":
        out["scope"] = scope(src.get("domains"))
        out["rules"] = rules(src.get("rulesOfEngagement"), prev.get("rules"))
    elif detail_state == "not-fetched" and prev.get("detail") in ("ok", "needs-join"):
        out["detail"] = prev["detail"]
        out["scope"] = prev.get("scope")
        out["rules"] = prev.get("rules")
    return out
