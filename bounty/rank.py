"""`programs rank` (BE-3, CONTRACTS §4, ADR 007). Pure scoring + text/JSON rendering."""

import math

from bounty.normalise import PAID_TIERS
from bounty.util import parse_time

CONF_SCORE = {"public": 1.0, "registered": 0.7, "application": 0.4}
SURFACE_WEIGHT = {"wildcard": 5.0, "cidr": 2.0, "url": 1.0, "domain": 1.0,
                  "android": 0.5, "ios": 0.5, "source": 0.5}
WEB_TYPES = ("wildcard", "domain", "url")
FACTORS = ("payout", "findability", "competition", "friction")
ASSUMED_FINDABILITY = 0.5   # an average program, when its scope is hidden (needs-join)
ASSUMED_FRICTION = 0.15     # same as "rate limit unknown"


def percentile_ranker(values):
    """pr(x): mid-rank percentile in 0..1 among `values` (lowest 0, highest 1, ties share the
    average position). A single value gets 0.5."""
    vals = sorted(v for v in values if v is not None)
    n = len(vals)

    def pr(x):
        if x is None or n == 0:
            return None
        if n == 1:
            return 0.5
        less = sum(1 for v in vals if v < x)
        eq = sum(1 for v in vals if v == x)
        pos = less + (eq - 1) / 2.0 if eq else less - 0.5
        return max(0.0, min(1.0, pos / (n - 1)))
    return pr


def wavg(pairs):
    """Weighted mean over the available values; missing (None) inputs are dropped and the remaining
    weights renormalised. None when nothing is available."""
    avail = [(w, v) for w, v in pairs if v is not None]
    tw = sum(w for w, _ in avail)
    if not avail or tw <= 0:
        return None
    return sum(w * v for w, v in avail) / tw


def _days_since(iso_text, now):
    if not iso_text:
        return None
    dt = parse_time(iso_text)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def inputs(p, now):
    b = p.get("bounty") or {}
    detail_ok = p.get("detail") == "ok"
    scope = p.get("scope") if detail_ok else None
    rules = p.get("rules") if detail_ok else None
    inp = {
        "detail": p.get("detail"),
        "max_eur": b.get("max_eur"),
        "min_eur": b.get("min_eur"),
        "confidentiality": p.get("confidentiality"),
        "first_seen_at": p.get("first_seen_at"),
        "days_since_first_seen": _days_since(p.get("first_seen_at"), now),
        "paid_share": None, "surface": None, "web_entries": None,
        "scope_changed_at": None, "days_since_scope_change": None,
        "safe_harbour": None, "rate_limit_rps": None, "automation": None,
        "intigriti_me": None, "user_agent": None, "request_header": None,
    }
    if scope is not None:
        ins = scope.get("in") or []
        if ins:
            inp["paid_share"] = sum(1 for e in ins if e.get("tier") in PAID_TIERS) / float(len(ins))
        inp["surface"] = sum(SURFACE_WEIGHT.get(e.get("type"), 0.0) for e in ins)
        inp["web_entries"] = sum(1 for e in ins if e.get("type") in WEB_TYPES)
        inp["scope_changed_at"] = scope.get("changed_at")
        inp["days_since_scope_change"] = _days_since(scope.get("changed_at"), now)
    if rules is not None:
        for k in ("safe_harbour", "rate_limit_rps", "automation", "intigriti_me", "user_agent",
                  "request_header"):
            inp[k] = rules.get(k)
    return inp


def exclusion(inp, include_no_bounty=False):
    if not include_no_bounty and inp["max_eur"] is not None and inp["max_eur"] == 0:
        return "no-bounty"
    if inp["safe_harbour"] is False:
        return "no-safe-harbour"
    if inp["detail"] == "ok" and not inp["web_entries"]:
        return "no-web-scope"
    return None


def _payout_base(inp):
    mx, mn = inp["max_eur"], inp["min_eur"]
    return wavg([(0.6, math.log1p(mx) if mx is not None else None),
                 (0.4, math.log1p(mn) if mn is not None else None)])


def friction(inp):
    if inp["detail"] != "ok":
        return None
    rps = inp["rate_limit_rps"]
    f = 0.0
    if rps is None:
        f += 0.15
    elif rps == 0:
        f += 0.6
    elif rps <= 2:
        f += 0.3
    if inp["intigriti_me"]:
        f += 0.05
    if inp["user_agent"]:
        f += 0.05
    return min(1.0, f)


def score(programs, weights, now, include_no_bounty=False, include_join=False):
    """Returns result dicts: ranked programs first (CONTRACTS §4 order), then excluded ones."""
    rows = []
    for p in programs:
        if p.get("status") != "open":
            continue
        inp = inputs(p, now)
        rows.append({"handle": p.get("handle"), "name": p.get("name"), "inputs": inp,
                     "excluded": exclusion(inp, include_no_bounty)})
    ranked = [r for r in rows if r["excluded"] is None]
    pr_pay = percentile_ranker([_payout_base(r["inputs"]) for r in ranked])
    pr_surf = percentile_ranker([math.log1p(r["inputs"]["surface"]) for r in ranked
                                 if r["inputs"]["surface"] is not None])
    pr_max = percentile_ranker([r["inputs"]["max_eur"] for r in ranked])
    w = weights
    wpos_all = w["payout"] + w["findability"] + w["competition"]
    for r in rows:
        inp = r["inputs"]
        flags = []
        if inp["detail"] == "needs-join":
            flags.append("join")
        elif inp["detail"] != "ok":
            flags.append("detail-" + str(inp["detail"]))
        if inp["automation"] == "forbidden":
            flags.append("no-auto")
        if inp["request_header"]:
            flags.append("hdr")
        r["flags"] = flags
        if r["excluded"]:
            r["total"], r["factors"], r["parts"] = None, {k: None for k in FACTORS}, {}
            continue
        parts = {}
        parts["pr_payout_base"] = pr_pay(_payout_base(inp))
        payout = parts["pr_payout_base"]
        if payout is not None and inp["paid_share"] is not None:
            payout *= 0.5 + 0.5 * inp["paid_share"]
        parts["pr_surface"] = pr_surf(math.log1p(inp["surface"])) if inp["surface"] is not None else None
        d = inp["days_since_scope_change"]
        parts["freshness"] = math.exp(-d / 90.0) if d is not None else None
        findability = wavg([(0.7, parts["pr_surface"]), (0.3, parts["freshness"])])
        parts["conf"] = CONF_SCORE.get(inp["confidentiality"])
        a = inp["days_since_first_seen"]
        parts["age"] = min(1.0, a / 730.0) if a is not None else None
        parts["pr_max_eur"] = pr_max(inp["max_eur"])
        competition = wavg([(0.5, parts["conf"]), (0.25, parts["age"]), (0.25, parts["pr_max_eur"])])
        fric = friction(inp)
        # Without the detail (needs-join, error) a whole factor is unknown. Assume an average
        # program rather than dropping the factor: dropping it let list-only programs outrank
        # programs we actually know (CONTRACTS §4).
        assumed = []
        if inp["detail"] != "ok":
            if findability is None:
                findability, assumed = ASSUMED_FINDABILITY, assumed + ["findability"]
            if fric is None:
                fric, assumed = ASSUMED_FRICTION, assumed + ["friction"]
        parts["assumed"] = assumed
        pos = [(w["payout"], payout), (w["findability"], findability),
               (w["competition"], None if competition is None else 1.0 - competition)]
        avg = wavg(pos)
        total = (avg * wpos_all if avg is not None else 0.0) - (w["friction"] * fric if fric is not None else 0.0)
        r["parts"] = parts
        r["factors"] = {"payout": payout, "findability": findability, "competition": competition,
                        "friction": fric}
        r["total"] = total

    def key(r):
        return (-r["total"], r["handle"] or "")
    ranked.sort(key=key)
    if not include_join:
        full = [r for r in ranked if r["inputs"]["detail"] == "ok"]
        rest = [r for r in ranked if r["inputs"]["detail"] != "ok"]
        ranked = full + rest
    excluded = sorted([r for r in rows if r["excluded"]], key=lambda r: (r["excluded"], r["handle"] or ""))
    return ranked + excluded


def _r4(x):
    return None if x is None else round(x, 4)


def to_json(results):
    out = []
    for r in results:
        inp = dict(r["inputs"])
        for k in ("paid_share", "days_since_first_seen", "days_since_scope_change"):
            inp[k] = _r4(inp[k])
        out.append({
            "handle": r["handle"],
            "total": _r4(r["total"]),
            "factors": {k: _r4(v) for k, v in r["factors"].items()},
            "inputs": inp,
            "flags": r["flags"],
            "excluded": r["excluded"],
        })
    return out


def _f(x, width=4, prec=2):
    return ("%*.*f" % (width, prec, x)) if x is not None else ("%*s" % (width, "-"))


def _money(x):
    if x is None:
        return "-"
    if x >= 10000:
        return "%dk" % round(x / 1000.0)
    return "%d" % round(x)


CONF_SHORT = {"public": "pub", "registered": "reg", "application": "app"}


def render_table(results, top=None):
    ranked = [r for r in results if not r["excluded"]]
    excluded = [r for r in results if r["excluded"]]
    shown = ranked[:top] if top else ranked
    hw = 34
    lines = ["%3s  %-*s %6s %5s %5s %5s %5s %6s %5s %6s %4s  %s" % (
        "#", hw, "handle", "total", "pay", "find", "comp", "fric", "max€", "surf", "scope", "conf", "flags")]
    for i, r in enumerate(shown, 1):
        inp, f = r["inputs"], r["factors"]
        h = r["handle"] if len(r["handle"]) <= hw else r["handle"][:hw - 1] + "…"
        surf = "-" if inp["surface"] is None else ("%g" % inp["surface"])
        days = "-" if inp["days_since_scope_change"] is None else "%dd" % inp["days_since_scope_change"]
        lines.append("%3d  %-*s %6s %5s %5s %5s %5s %6s %5s %6s %4s  %s" % (
            i, hw, h, _f(r["total"], 6, 3), _f(f["payout"], 5), _f(f["findability"], 5),
            _f(f["competition"], 5), _f(f["friction"], 5), _money(inp["max_eur"]), surf, days,
            CONF_SHORT.get(inp["confidentiality"], inp["confidentiality"] or "-"), ",".join(r["flags"])))
    if top and len(ranked) > top:
        lines.append("… %d more ranked (no --top to see all)" % (len(ranked) - top))
    if excluded:
        by = {}
        for r in excluded:
            by.setdefault(r["excluded"], []).append(r["handle"])
        lines.append("")
        if top:
            lines.append("excluded: " + ", ".join("%s %d" % (k, len(v)) for k, v in sorted(by.items())))
        else:
            for k, v in sorted(by.items()):
                lines.append("excluded %s (%d):" % (k, len(v)))
                lines.extend(_wrap(v, 116, "  "))
    return "\n".join(lines)


def _wrap(items, width, indent):
    out, cur = [], indent
    for it in items:
        add = it if cur == indent else ", " + it
        if len(cur) + len(add) > width and cur != indent:
            out.append(cur + ",")
            cur = indent + it
        else:
            cur += add
    if cur != indent:
        out.append(cur)
    return out


def render_explain(results, handle, weights):
    r = next((x for x in results if x["handle"] == handle), None)
    if r is None:
        return None
    inp, p, f = r["inputs"], r.get("parts") or {}, r["factors"]
    rank = [x for x in results if not x["excluded"]]
    pos = next((i for i, x in enumerate(rank, 1) if x["handle"] == handle), None)
    L = ["%s  (%s)" % (handle, r.get("name") or "")]
    if r["excluded"]:
        L.append("excluded: %s" % r["excluded"])
    else:
        L.append("rank %d of %d, total %.3f" % (pos, len(rank), r["total"]))
    L.append("flags: %s" % (", ".join(r["flags"]) or "-"))
    L.append("inputs:")
    for k in sorted(inp):
        v = inp[k]
        if isinstance(v, float):
            v = round(v, 4)
        L.append("  %-24s %s" % (k, "-" if v is None else v))
    if not r["excluded"]:
        def g(x):
            return "-" if x is None else "%.3f" % x
        L.append("factors (missing inputs dropped, weights inside a factor renormalised):")
        L.append("  payout       %s = pr(0.6·ln(1+max_eur)+0.4·ln(1+min_eur)) %s × (0.5+0.5·paid_share %s)"
                 % (g(f["payout"]), g(p.get("pr_payout_base")), g(inp["paid_share"])))
        L.append("  findability  %s = 0.7·pr(ln(1+surface)) %s + 0.3·freshness %s"
                 % (g(f["findability"]), g(p.get("pr_surface")), g(p.get("freshness"))))
        L.append("  competition  %s = 0.5·conf %s + 0.25·age %s + 0.25·pr(max_eur) %s"
                 % (g(f["competition"]), g(p.get("conf")), g(p.get("age")), g(p.get("pr_max_eur"))))
        L.append("  friction     %s (automation forbidden 0.6, rps<=2 0.3, rps unknown 0.15, "
                 "intigriti_me 0.05, user_agent 0.05; cap 1)" % g(f["friction"]))
        L.append("weights: " + ", ".join("%s %.2f" % (k, weights[k]) for k in FACTORS))
        L.append("total = w.payout·payout + w.findability·findability + w.competition·(1−competition)"
                 " − w.friction·friction")
        if r.get("parts", {}).get("assumed"):
            L.append("  (scope hidden until you join: assumed %s = average)" % " and ".join(r["parts"]["assumed"]))
    return "\n".join(L)
