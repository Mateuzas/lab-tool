"""Recon: the "look here" list (CONTRACTS §10, BE-18).

  recon passive <handle> [--domain D]   certificate transparency (crt.sh) + one DNS lookup per in-scope
                                        name with the Mac's resolver. Nothing is sent to the program's
                                        own hosts, so it runs automatically (after a sync, on `u`).
  recon map [--limit N]                 the guard's ACTIVE program only, on a key press: bounty-map in
                                        kali-bounty (httpx-toolkit + katana through the guard).
  recon exposures [--limit N]           the ACTIVE program only, on a key press: kali-lab's curated
                                        nuclei checks on hosts a map found live, through the guard.
  recon list <handle> [--json]          hosts + live + urls + exposures merged into one row per host.

Files in work/<handle>/recon/ (CONTRACTS §10):
  hosts.jsonl   one line per name (passive; this module writes it)
  live.jsonl    httpx results per host (bounty-map writes it)
  urls.jsonl    katana URLs (bounty-map)
  mapped.json   {"hosts": {host: ts}}: hosts bounty-map probed, so "no answer" reads as not live
  exposures.json {"checked": {host: ts}, "hits": [...]}: bounty-exposures (no bodies kept)
  state.json    {"baseline_at": first passive run}: hosts found by that first run are not "new"

Names are classified with the guard's own matcher (guard_policy.py, imported by path), so the
list can never call a host in scope that the guard would refuse.
"""

import concurrent.futures
import datetime
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from bounty import guard, shell
from bounty.util import REPO_ROOT, BountyError, iso, parse_time, read_json, utcnow, write_bytes, write_json

CRTSH = "https://crt.sh/?q=%25.{domain}&output=json"
CRTSH_GAP = 5.0          # ≥ 5 s between crt.sh requests
CRTSH_TIMEOUT = 60
USER_AGENT = "kali-lab-recon/1"   # generic on purpose: no user name leaves the Mac
RESOLVE_WORKERS = 16
NEW_DAYS = 7
SCOPE_WORD = {None: "in-scope", "out-of-scope": "out-of-scope"}

# Signals (CONTRACTS §10.3): one table. `words` match whole tokens of the host, URL paths, title and
# tech; `parts` match anywhere in that text.
SIGNALS = (
    ("login", {"login", "signin", "logon", "auth", "oauth", "oauth2", "sso", "saml", "signup",
               "register", "account", "accounts", "password", "reset"}, ("sign-in", "log-in")),
    ("api", {"api", "apis", "rest", "rpc", "jsonrpc", "v1", "v2", "v3"}, ()),
    ("admin", {"admin", "administrator", "dashboard", "manage", "management", "console", "internal",
               "staff", "backoffice", "cms", "panel"}, ()),
    ("upload", {"upload", "uploads", "import", "attachment", "attachments", "files", "media"}, ()),
    ("graphql", {"graphql", "gql", "graphiql"}, ()),
    ("swagger", {"swagger", "openapi", "redoc", "apidocs"}, ("api-docs", "swagger-ui")),
    ("debug", {"debug", "trace", "actuator", "phpinfo", "profiler", "test", "staging", "stage",
               "dev", "sandbox", "qa", "uat", "preprod"}, ("_profiler",)),
)
EXPOSURE_BONUS = 0.3     # a curated exposure hit outweighs any name signal (CONTRACTS §10.6)
JS_FRAMEWORKS = ("react", "angular", "vue", "next.js", "nuxt", "svelte", "ember")
JS_HEAVY_URLS = 20
_TOKEN = re.compile(r"[a-z0-9]+")


# -- shared ------------------------------------------------------------------------------------

def _policy_module():
    p = os.path.join(REPO_ROOT, "container", "share", "bounty", "guard_policy.py")
    spec = importlib.util.spec_from_file_location("guard_policy", p)
    gp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gp)
    return gp


def load_profile(ws, handle):
    gp = _policy_module()
    if not isinstance(handle, str) or not gp.HANDLE_RE.match(handle):
        raise BountyError("usage", "bad handle %r" % handle, 64)
    profile = read_json(os.path.join(ws, "programs", handle, "program.json"))
    if profile is None:
        raise BountyError("usage", "no program %s (programs sync, or manual add)" % handle, 64)
    try:
        policy = gp.Policy(profile, "check", None, handle)
    except gp.PolicyError as e:
        raise BountyError("profile", "%s can't be enforced: %s" % (handle, e.message), 4)
    return profile, policy


def classify(policy, host):
    reason, _category = policy.host_decision(host)
    return SCOPE_WORD.get(reason, "not-in-scope")


def recon_dir(ws, handle):
    return os.path.join(ws, "work", handle, "recon")


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    v = json.loads(line)
                except ValueError:
                    continue   # a line cut short by an interrupted run never breaks the list
                if isinstance(v, dict):
                    out.append(v)
    except FileNotFoundError:
        pass
    return out


def _write_jsonl(path, records):
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    write_bytes(path, data.encode("utf-8"))


def _log(line):
    sys.stderr.write("bounty: " + line + "\n")
    sys.stderr.flush()


# -- passive -----------------------------------------------------------------------------------

def passive_domains(profile, domain=None):
    """Base domains for crt.sh: every in-scope wildcard (`*.x.com` -> x.com), or --domain."""
    if domain:
        return [domain.strip().lower().lstrip("*.").rstrip(".")]
    out = []
    for e in (profile.get("scope") or {}).get("in") or []:
        v = (e.get("value") or "").strip().lower()
        if e.get("type") == "wildcard" and v.startswith("*.") and v[2:] not in out:
            out.append(v[2:].rstrip("."))
    return out


def scope_hosts(profile):
    """In-scope `domain` and `url` entries: hosts that are in scope by name (source "scope")."""
    out = []
    for e in (profile.get("scope") or {}).get("in") or []:
        t, v = e.get("type"), (e.get("value") or "").strip().lower()
        host = None
        if t == "domain":
            host = v
        elif t == "url":
            host = urllib.parse.urlsplit(v if "://" in v else "https://" + v).hostname
        if host and "*" not in host and host not in out:
            out.append(host.rstrip("."))
    return out


def names_from_crtsh(raw, domain):
    """crt.sh JSON -> sorted names under `domain` (wildcards reduced to their base)."""
    try:
        rows = json.loads(raw)
    except ValueError:
        raise BountyError("crtsh", "crt.sh returned something that is not JSON", 3)
    names = set()
    suffix = "." + domain
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        for field in ("name_value", "common_name"):
            for n in str(r.get(field) or "").split("\n"):
                n = n.strip().lower().rstrip(".")
                while n.startswith("*."):
                    n = n[2:]
                if not n or " " in n or "@" in n or "*" in n:
                    continue
                if n == domain or n.endswith(suffix):
                    names.add(n)
    return sorted(names)


def fetch_crtsh(domain, opener=None, sleep=time.sleep):
    """One crt.sh query, retried once after CRTSH_GAP. Raises BountyError(crtsh, exit 3)."""
    url = CRTSH.format(domain=urllib.parse.quote(domain))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    opener = opener or urllib.request.urlopen
    last = None
    for attempt in (1, 2):
        try:
            with opener(req, timeout=CRTSH_TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # urllib raises several kinds; all mean "try once more, then give up"
            last = e
            if attempt == 1:
                sleep(CRTSH_GAP)
    raise BountyError("crtsh", "crt.sh failed for %s: %s" % (domain, last), 3)


def resolve(name):
    """(resolves, sorted ips) with the Mac's resolver."""
    try:
        infos = socket.getaddrinfo(name, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False, []
    return True, sorted({i[4][0] for i in infos})


def merge_hosts(existing, found, now):
    """existing: host -> record; found: host -> {"sources", "scope", "resolves", "ips"}."""
    for host, f in found.items():
        rec = existing.get(host)
        if rec is None:
            rec = {"host": host, "sources": [], "first_seen": now}
            existing[host] = rec
        rec["sources"] = sorted(set(rec.get("sources") or []) | set(f["sources"]))
        rec["last_seen"] = now
        rec["scope"] = f["scope"]
        if f.get("checked"):
            rec["resolves"], rec["ips"] = f["resolves"], f["ips"]
        else:
            rec.setdefault("resolves", None)
            rec.setdefault("ips", [])
    return existing


def passive(ws, handle, domain=None, out=sys.stdout, fetch=fetch_crtsh, resolver=resolve, sleep=time.sleep):
    profile, policy = load_profile(ws, handle)
    domains = passive_domains(profile, domain)
    if domain and classify(policy, "x." + domains[0]) != "in-scope" and classify(policy, domains[0]) != "in-scope":
        raise BountyError("usage", "%s is not in %s's scope" % (domains[0], handle), 64)
    now = iso(utcnow())
    found = {}

    def add(host, source):
        f = found.setdefault(host, {"sources": set(), "scope": classify(policy, host)})
        f["sources"].add(source)

    for h in scope_hosts(profile):
        add(h, "scope")
    failed = []
    for i, d in enumerate(domains):
        _log("progress: %d/%d %s" % (i, len(domains), d))
        if i:
            sleep(CRTSH_GAP)
        try:
            for n in names_from_crtsh(fetch(d), d):
                add(n, "crt.sh")
        except BountyError as e:
            failed.append(e.message)
            _log("crtsh: " + e.message)
    if domains:
        _log("progress: %d/%d done" % (len(domains), len(domains)))

    # One lookup per in-scope name; out-of-scope names are recorded but never looked up.
    todo = sorted(h for h, f in found.items() if f["scope"] == "in-scope")
    with concurrent.futures.ThreadPoolExecutor(RESOLVE_WORKERS) as pool:
        for host, (ok, ips) in zip(todo, pool.map(resolver, todo)):
            found[host].update(checked=True, resolves=ok, ips=ips)

    d = recon_dir(ws, handle)
    path = os.path.join(d, "hosts.jsonl")
    existing = {r["host"]: r for r in _read_jsonl(path) if isinstance(r.get("host"), str)}
    merge_hosts(existing, found, now)
    _write_jsonl(path, [existing[h] for h in sorted(existing)])
    state_path = os.path.join(d, "state.json")
    state = read_json(state_path, {}) or {}
    if not state.get("baseline_at"):
        state["baseline_at"] = now
    state["passive_at"] = now
    write_json(state_path, state)

    inscope = [f for f in found.values() if f["scope"] == "in-scope"]
    out.write("%s: %d names (%d in scope, %d resolve) from %s\n" % (
        handle, len(found), len(inscope), sum(1 for f in inscope if f.get("resolves")),
        ", ".join(domains) or "the scope entries"))
    if failed:
        raise BountyError("crtsh", "partial results kept; " + "; ".join(failed), 3)
    return 0


# -- list --------------------------------------------------------------------------------------

def _tokens(text):
    return set(_TOKEN.findall(text.lower()))


def _registrable(host):
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:])


def signals_for(host, live, urls):
    """Signals from the host name, URL paths, title and tech (CONTRACTS §10.3)."""
    paths = [urllib.parse.urlsplit(u.get("url") or "").path for u in urls]
    title = (live or {}).get("title") or ""
    tech = [str(t) for t in (live or {}).get("tech") or []]
    sub = ".".join(host.split(".")[:-2])   # the program's own domain name is never a signal
    text = " ".join([sub, title, " ".join(tech)] + paths).lower()
    toks = _tokens(text)
    out = []
    for name, words, parts in SIGNALS:
        if toks & words or any(p in text for p in parts):
            out.append(name)
    js = sum(1 for p in paths if p.lower().endswith(".js"))
    if js >= JS_HEAVY_URLS or any(f in t.lower() for t in tech for f in JS_FRAMEWORKS):
        out.append("js-heavy")
    target = (live or {}).get("location") or (live or {}).get("final_url") or ""
    th = urllib.parse.urlsplit(target).hostname if target else None
    if th and th.lower() != host and _registrable(th) != _registrable(host):
        out.append("redirect-offsite")
    return out


def interest(new, live, signals, exposed=False):
    return round(min(1.0, (0.35 if new else 0) + (0.15 if live else 0) + 0.1 * len(signals)
                     + (EXPOSURE_BONUS if exposed else 0)), 2)


def _best_live(records):
    """The record to show for a host: a real answer over a refusal, https over http."""
    def key(r):
        return (r.get("status") is None, not str(r.get("url") or "").startswith("https://"))
    return sorted(records, key=key)[0] if records else None


def rows(ws, handle, now=None):
    profile, policy = load_profile(ws, handle)
    d = recon_dir(ws, handle)
    hosts = {r["host"]: r for r in _read_jsonl(os.path.join(d, "hosts.jsonl")) if isinstance(r.get("host"), str)}
    live = {}
    for r in _read_jsonl(os.path.join(d, "live.jsonl")):
        if isinstance(r.get("host"), str):
            live.setdefault(r["host"], []).append(r)
    urls = {}
    for r in _read_jsonl(os.path.join(d, "urls.jsonl")):
        if isinstance(r.get("host"), str):
            urls.setdefault(r["host"], []).append(r)
    mapped = (read_json(os.path.join(d, "mapped.json"), {}) or {}).get("hosts") or {}
    exposures = {}
    for h in (read_json(os.path.join(d, "exposures.json"), {}) or {}).get("hits") or []:
        if isinstance(h, dict) and isinstance(h.get("host"), str) and h.get("check"):
            exposures.setdefault(h["host"], [])
            if h["check"] not in exposures[h["host"]]:
                exposures[h["host"]].append(h["check"])
    state = read_json(os.path.join(d, "state.json"), {}) or {}
    now = now or utcnow()
    baseline = state.get("baseline_at")
    cutoff = now - datetime.timedelta(days=NEW_DAYS)

    out = []
    for host in sorted(set(hosts) | set(live)):
        rec = hosts.get(host) or {}
        first = rec.get("first_seen")
        new = False
        if first:
            try:
                t = parse_time(first)
                new = t >= cutoff and (baseline is None or first > baseline)
            except BountyError:
                pass
        best = _best_live(live.get(host) or [])
        if best is not None and best.get("status") is not None:
            is_live = True
        elif host in mapped:
            is_live = False
        else:
            is_live = None
        u = urls.get(host) or []
        sig = signals_for(host, best, u)
        exp = sorted(exposures.get(host) or [])
        if exp:
            sig.append("exposed")
        out.append({
            "host": host,
            "scope": rec.get("scope") or classify(policy, host),
            "new": new,
            "live": is_live,
            "status": best.get("status") if best else None,
            "title": (best.get("title") or None) if best else None,
            "tech": list(best.get("tech") or []) if best else [],
            "urls": len({(x.get("method") or "GET", x.get("url")) for x in u}),
            "signals": sig,
            "exposures": exp,
            "interest": interest(new, is_live, [x for x in sig if x != "exposed"], bool(exp)),
            "first_seen": first,
        })
    # interest desc, then first_seen desc (unknown last), then host: stable sorts, last key first
    out.sort(key=lambda r: r["host"])
    out.sort(key=lambda r: r["first_seen"] or "", reverse=True)
    out.sort(key=lambda r: -r["interest"])
    for r in out:
        del r["first_seen"]
    return out


def list_cmd(ws, handle, as_json=False, out=sys.stdout):
    rs = rows(ws, handle)
    if as_json:
        out.write(json.dumps(rs, ensure_ascii=False) + "\n")
        return 0
    if not rs:
        out.write("no hosts yet for %s: run `host/bounty recon passive %s`\n" % (handle, handle))
        return 0
    for r in rs:
        flags = ("new " if r["new"] else "") + {True: "live", False: "dead", None: ""}[r["live"]]
        out.write("%4.2f  %-45s %-12s %-4s %-30s %s %s\n" % (
            r["interest"], r["host"][:45], r["scope"], r["status"] or "", (r["title"] or "")[:30],
            ",".join(r["signals"]), flags.strip()))
    return 0


# -- map ---------------------------------------------------------------------------------------

def map_args(container, handle, limit=None, script="bounty-map"):
    args = ["exec", "-e", "BOUNTY_HANDLE=" + handle, "-w", "%s/%s" % (shell.WORK, handle),
            container, "bounty-run", "--actor", "human", "--", script]
    if limit:
        args += ["--limit", str(int(limit))]
    return args


def automation_allowed(profile):
    rules = profile.get("rules") or {}
    rps = rules.get("rate_limit_rps")
    return not (rules.get("automation") == "forbidden" or rps == 0)


def map_cmd(ws, limit=None, run=subprocess.call, script="bounty-map"):
    """Active mapping of the guard's active program (CONTRACTS §10.2), or with script
    bounty-exposures the curated exposure checks (§10.6). Exit: the script's own code;
    3 guard not running; 5 no active program; 6 the program forbids automated tools."""
    cfg = guard._cfg()
    if not guard.running(cfg["container"]):
        raise BountyError("guard", "%s is not running: start the guard first" % cfg["container"], 3)
    handle = shell.active_program(cfg)
    if not handle:
        raise BountyError("no-program", "the guard has no active program: pick one first (program use)", 5)
    profile, _policy = load_profile(ws, handle)
    if not automation_allowed(profile):
        raise BountyError("automation-forbidden",
                          "%s forbids automated tools: check it by hand in Burp" % handle, 6)
    guard._docker(shell.mkdir_args(cfg["container"], handle), check=True)
    return run(["docker"] + map_args(cfg["container"], handle, limit, script))


def exposures_cmd(ws, limit=None, run=subprocess.call):
    return map_cmd(ws, limit, run, script="bounty-exposures")
