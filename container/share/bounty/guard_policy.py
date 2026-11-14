"""Scope guard decision logic (CONTRACTS §3.1, §3.2, §3.4). Pure Python, stdlib only.

No mitmproxy import here: the addon (guard_addon.py), bounty-ctl, bounty-run and the Mac-side
`host/bounty program use` all use this module, and its unit tests run on the Mac's python3 (3.9)
and the container's. Everything fails closed: a profile that does not parse is refused whole.
"""

import ipaddress
import posixpath
import re

try:  # py3
    from urllib.parse import unquote, urlsplit
except ImportError:  # pragma: no cover
    raise

REASONS = ("no-profile", "host-mismatch", "out-of-scope", "not-in-scope", "internal-address",
           "rate-limited", "automation-forbidden")

DEFAULT_RPS = 2.0          # rules.rate_limit_rps == null (CONTRACTS §3.1 step 6)
MAX_WAIT_S = 10.0          # wait up to 10 s for a token, then rate-limited
DEFAULT_RECON_RPS = 2.0
MISSING_UA = "kali-lab"
ACTOR_HEADER = "X-Bounty-Actor"
GUARD_HEADER = "X-Bounty-Guard"
ACTOR_RE = re.compile(r"^(human|claude|tool:[a-z0-9][a-z0-9._+-]{0,39})$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z][a-z0-9-]{0,62}$")
PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}|<[^<>]*>")

# Placeholders, compared after lower-casing and dropping spaces, '-' and '_' (CONTRACTS §3.1 step 7).
PH_USERNAME = ("{username}", "<username>")
PH_USERAGENT = ("{useragent}", "<useragent>")

# Internal ranges (step 5): private, loopback, link-local (incl. 169.254.169.254 metadata), CGNAT,
# plus "this network", multicast and reserved, which are never a public web target either.
_INTERNAL_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4")]
_INTERNAL_V6 = [ipaddress.ip_network(n) for n in (
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")]
INTERNAL_RANGES = [str(n) for n in _INTERNAL_V4 + _INTERNAL_V6]


class PolicyError(Exception):
    """A profile, template or config that cannot be enforced. exit_code: 4 parse/template, 2 username."""

    def __init__(self, message, exit_code=4):
        Exception.__init__(self, message)
        self.message = message
        self.exit_code = exit_code


# ---------------------------------------------------------------- hosts and addresses

def norm_host(host):
    """Lower case, no trailing dot, no brackets, IDNA (punycode) form; IPs in canonical form.
    Returns '' for something that is not a usable host."""
    if host is None:
        return ""
    h = str(host).strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".").lower()
    if not h:
        return ""
    ip = parse_ip(h)
    if ip is not None:
        return str(ip)
    try:
        h = h.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    return h


def split_host_port(value):
    """'host:443' / '[::1]:443' / 'host' -> (host, port or None)."""
    v = (value or "").strip()
    if v.startswith("["):
        end = v.find("]")
        if end > 0:
            rest = v[end + 1:]
            port = rest[1:] if rest.startswith(":") else ""
            return v[1:end], int(port) if port.isdigit() else None
    if v.count(":") == 1:
        h, p = v.split(":")
        return h, int(p) if p.isdigit() else None
    return v, None


def parse_ip(text):
    try:
        return ipaddress.ip_address(str(text).strip().strip("[]"))
    except ValueError:
        return None


def is_internal(ip):
    """True for addresses that are never a public target (step 5)."""
    ip = parse_ip(ip) if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ip
    if ip is None:
        return True
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return is_internal(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_internal(ip.sixtofour)
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return is_internal(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        return any(ip in n for n in _INTERNAL_V6)
    return any(ip in n for n in _INTERNAL_V4)


def is_ip_literal(host):
    return parse_ip(host) is not None


# ---------------------------------------------------------------- paths

def _path_variants(raw_path):
    """Paths a server might act on for raw_path: query dropped, percent-decoded zero to three
    times, backslashes as slashes, repeated slashes collapsed, dot segments resolved."""
    p = (raw_path or "/").split("#", 1)[0].split("?", 1)[0] or "/"
    out = set()
    cur = p
    for _ in range(4):
        c = re.sub(r"/{2,}", "/", cur.replace("\\", "/"))
        if not c.startswith("/"):
            c = "/" + c
        out.add(posixpath.normpath(c).replace("//", "/"))
        dec = unquote(cur)
        if dec == cur:
            break
        cur = dec
    return out


def path_matches(prefix, raw_path, out=False):
    """Path-prefix match on segment boundaries: /paper matches /paper and /paper/x, not /paperclip.
    In-scope (out=False): every decoded form must match, case-sensitive, so /paper/%2e%2e/admin
    does not count as /paper. Out-of-scope (out=True): any form matching, ignoring case, is enough."""
    pre = (prefix or "").split("?", 1)[0]
    if not pre.startswith("/"):
        pre = "/" + pre
    base = pre.rstrip("/")
    if not base:
        return True
    variants = _path_variants(raw_path)

    def m(v):
        a, b = (v.lower(), base.lower()) if out else (v, base)
        return a == b or a.startswith(b + "/")
    return any(m(v) for v in variants) if out else all(m(v) for v in variants)


# ---------------------------------------------------------------- scope entries

class Entry(object):
    """One enforceable scope entry. kind: wildcard | domain | url | cidr | glob."""

    __slots__ = ("kind", "host", "path", "scheme", "port", "network", "source")

    def __init__(self, kind, host="", path="", scheme="", port=None, network=None, source=""):
        self.kind = kind
        self.host = host
        self.path = path
        self.scheme = scheme
        self.port = port
        self.network = network
        self.source = source

    def __repr__(self):
        return "Entry(%s %s)" % (self.kind, self.source)


def _host_pattern_matches(pattern, host, out):
    """pattern is '*.example.com', a glob (out only) or an exact host."""
    if pattern.startswith("*.") and "*" not in pattern[2:]:
        base = pattern[2:]
        if host.endswith("." + base):
            return True
        return out and host == base   # out-of-scope wildcards also cover the apex (safer)
    if "*" in pattern:
        if not out:
            return False
        rx = "^" + ".*".join(re.escape(p) for p in pattern.split("*")) + "$"
        return re.match(rx, host) is not None
    return host == pattern


def _parse_url_value(value):
    """'https://Host:8443/path' or 'host/path' -> (scheme, host, port, path). Host keeps a '*'."""
    v = value.strip()
    if "://" not in v:
        v = "//" + v
    parts = urlsplit(v)
    try:
        port = parts.port
    except ValueError:
        raise PolicyError("bad port in url scope entry %r" % value)
    host = (parts.hostname or "").rstrip(".")
    if "*" not in host:
        host = norm_host(host)
    return parts.scheme.lower(), host, port, parts.path or ""


def compile_entry(e, out):
    """dict from program.json -> Entry, or None when the type is not enforceable (shown only)."""
    if not isinstance(e, dict):
        raise PolicyError("scope entry is not an object: %r" % (e,))
    typ = e.get("type")
    val = e.get("value")
    if not isinstance(typ, str) or not isinstance(val, str):
        raise PolicyError("scope entry without string type/value: %r" % (e,))
    typ = typ.lower()
    v = val.strip()
    if not v:
        return None
    if typ in ("cidr", "ip"):
        try:
            net = ipaddress.ip_network(v, strict=False)
        except ValueError:
            raise PolicyError("bad %s scope entry %r" % (typ, val))
        return Entry("cidr", network=net, source=val)
    if typ == "domain" and ("/" in v or ":" in v.strip("[]")) and not is_ip_literal(v):
        typ = "url"   # a domain value that carries a scheme, port or path is really a url
    if typ == "wildcard":
        h = v.lower().rstrip(".")
        if "://" in h or "/" in h:
            scheme, host, port, path = _parse_url_value(v)
            h = host
        if h.startswith("*.") and "*" not in h[2:] and HOSTNAME_RE.match(h[2:]):
            return Entry("wildcard", host=h, source=val)
        if out and "*" in h:
            return Entry("glob", host=h, source=val)
        if "*" not in h and HOSTNAME_RE.match(h):
            return Entry("domain", host=h, source=val)   # "wildcard" without a star: exact host
        return None   # e.g. "*acme*" in scope.in: not enforceable, so it allows nothing
    if typ == "domain":
        h = norm_host(v)
        if "*" in h:
            return compile_entry({"type": "wildcard", "value": v}, out)
        if is_ip_literal(h) or HOSTNAME_RE.match(h):
            return Entry("domain", host=h, source=val)
        raise PolicyError("bad domain scope entry %r" % val)
    if typ == "url":
        scheme, host, port, path = _parse_url_value(v)
        if not host:
            raise PolicyError("url scope entry without host %r" % val)
        if scheme and scheme not in ("http", "https"):
            return None
        if "*" in host:
            if host.startswith("*.") and "*" not in host[2:]:
                pass
            elif not out:
                return None
        elif not (is_ip_literal(host) or HOSTNAME_RE.match(host)):
            raise PolicyError("bad url scope entry %r" % val)
        return Entry("url", host=host, path=path, scheme=scheme, port=port, source=val)
    if typ == "other" and out:
        # Out-of-scope "other" entries that are really a hostname (Dropbox: replay.dropbox.com style)
        # are enforced; anything else (app names) is shown only.
        h = v.lower().rstrip(".")
        if h.startswith("*.") and HOSTNAME_RE.match(h[2:]):
            return Entry("wildcard", host=h, source=val)
        if HOSTNAME_RE.match(h):
            return Entry("domain", host=h, source=val)
    return None


def entry_matches_host(entry, host, out):
    """Host-level match (CONNECT / before the path is known)."""
    if entry.kind == "cidr":
        ip = parse_ip(host)
        return ip is not None and ip.version == entry.network.version and ip in entry.network
    if entry.kind in ("wildcard", "glob"):
        return _host_pattern_matches(entry.host, host, out)
    if entry.kind in ("domain", "url"):
        return _host_pattern_matches(entry.host, host, out)
    return False


def entry_matches(entry, host, scheme, port, path, out):
    """Full match once the request line is known."""
    if not entry_matches_host(entry, host, out):
        return False
    if entry.kind != "url":
        return True
    if not out:
        if entry.scheme and scheme and entry.scheme != scheme:
            return False
        if entry.port is not None and port is not None and entry.port != port:
            return False
    return path_matches(entry.path, path, out=out)


def entry_whole_host(entry):
    """An out-of-scope entry that excludes the whole host (no path part)."""
    return entry.kind != "url" or not entry.path or entry.path == "/"


# ---------------------------------------------------------------- templates (step 7)

def _ph_key(token):
    return re.sub(r"[\s_-]+", "", token.lower())


def template_placeholders(template):
    return PLACEHOLDER_RE.findall(template or "")


def check_template(template, what):
    """Raise PolicyError(exit 4) for an unknown placeholder. Returns (needs_username, has_ua_slot)."""
    needs_user = has_ua = False
    for tok in template_placeholders(template):
        k = _ph_key(tok)
        if k in PH_USERNAME:
            needs_user = True
        elif k in PH_USERAGENT:
            if what != "user_agent":
                raise PolicyError("%s template %r: %s only makes sense in user_agent" % (what, template, tok))
            has_ua = True
        else:
            raise PolicyError("%s template %r has an unknown placeholder %s" % (what, template, tok))
    return needs_user, has_ua


def fill_template(template, username, original_ua=None):
    def sub(m):
        k = _ph_key(m.group(0))
        if k in PH_USERNAME:
            return username
        if k in PH_USERAGENT:
            return original_ua if original_ua else MISSING_UA
        raise PolicyError("unknown placeholder %s" % m.group(0))
    return PLACEHOLDER_RE.sub(sub, template)


def _safe_header_value(v):
    return not any(c in v for c in "\r\n\x00")


class Identification(object):
    """Program identification: one request header and/or a User-Agent template (step 7)."""

    def __init__(self, rules, username):
        self.header_name = None
        self.header_template = None
        self.ua_template = None
        self.needs_username = False
        rh = rules.get("request_header")
        ua = rules.get("user_agent")
        if rh is not None:
            if not isinstance(rh, str) or ":" not in rh:
                raise PolicyError("request_header template %r is not 'Name: value'" % (rh,))
            name, value = rh.split(":", 1)
            name, value = name.strip(), value.strip()
            if not HEADER_NAME_RE.match(name) or not value:
                raise PolicyError("request_header template %r is not 'Name: value'" % (rh,))
            if name.lower() in ("host", "user-agent", ACTOR_HEADER.lower(), "content-length",
                                "transfer-encoding", "connection"):
                raise PolicyError("request_header template sets %s, which the guard does not allow" % name)
            nu, _ = check_template(value, "request_header")
            self.needs_username |= nu
            self.header_name, self.header_template = name, value
        if ua is not None:
            if not isinstance(ua, str) or not ua.strip():
                raise PolicyError("user_agent template %r is empty" % (ua,))
            nu, _ = check_template(ua, "user_agent")
            self.needs_username |= nu
            self.ua_template = ua.strip()
        if self.needs_username:
            if not username:
                raise PolicyError("the profile's identification needs INTIGRITI_USERNAME, which is not set",
                                  exit_code=2)
            if not USERNAME_RE.match(username):
                raise PolicyError("INTIGRITI_USERNAME has characters that cannot go in a header",
                                  exit_code=2)
        self.username = username or ""
        for t in (self.header_template, self.ua_template):
            if t is not None and not _safe_header_value(fill_template(t, self.username, "x")):
                raise PolicyError("identification template produces an invalid header")

    def headers(self, original_ua):
        """{name: value} to set (replacing any existing header of that name)."""
        out = {}
        if self.header_name:
            out[self.header_name] = fill_template(self.header_template, self.username)
        if self.ua_template:
            ua = original_ua if original_ua and _safe_header_value(original_ua) else None
            out["User-Agent"] = fill_template(self.ua_template, self.username, ua)
        return out

    def describe(self):
        return {"request_header": self.header_name, "user_agent": self.ua_template is not None}


# ---------------------------------------------------------------- recon sources (§3.4)

def compile_recon(cfg):
    """{"hosts": [...], "rate_limit_rps": 2, "egress_ip_url": "..."} -> normalised dict. Exact hosts."""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise PolicyError("recon-sources.json is not an object")
    hosts = cfg.get("hosts", [])
    if not isinstance(hosts, list):
        raise PolicyError("recon-sources.json: hosts is not a list")
    out = []
    for h in hosts:
        if not isinstance(h, str):
            raise PolicyError("recon-sources.json: host %r is not a string" % (h,))
        n = norm_host(h)
        if "*" in h or not (HOSTNAME_RE.match(n) or is_ip_literal(n)):
            raise PolicyError("recon-sources.json: %r is not an exact host" % h)
        out.append(n)
    rps = cfg.get("rate_limit_rps", DEFAULT_RECON_RPS)
    if isinstance(rps, bool) or not isinstance(rps, (int, float)) or rps <= 0:
        raise PolicyError("recon-sources.json: rate_limit_rps must be > 0")
    url = cfg.get("egress_ip_url")
    if url is not None:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PolicyError("recon-sources.json: egress_ip_url must be an https URL")
        if norm_host(urlsplit(url).hostname) not in out:
            raise PolicyError("recon-sources.json: egress_ip_url host is not in hosts")
    return {"hosts": sorted(set(out)), "rate_limit_rps": float(rps), "egress_ip_url": url}


# ---------------------------------------------------------------- the policy

class Policy(object):
    """A compiled, validated program profile plus recon sources."""

    def __init__(self, profile, username=None, recon=None, handle=None):
        if not isinstance(profile, dict):
            raise PolicyError("program.json is not an object")
        h = profile.get("handle")
        if not isinstance(h, str) or not HANDLE_RE.match(h):
            raise PolicyError("program.json has no valid handle")
        if handle is not None and h != handle:
            raise PolicyError("program.json handle %r does not match the active handle %r" % (h, handle))
        self.handle = h
        scope = profile.get("scope")
        rules = profile.get("rules")
        if not isinstance(scope, dict) or not isinstance(rules, dict):
            raise PolicyError("%s has no scope/rules (detail: %s); run programs sync after joining"
                              % (h, profile.get("detail")))
        ins, outs = scope.get("in"), scope.get("out")
        if not isinstance(ins, list) or not isinstance(outs, list):
            raise PolicyError("%s: scope.in/scope.out are not lists" % h)
        self.scope_in = [x for x in (compile_entry(e, False) for e in ins) if x is not None]
        self.scope_out = [x for x in (compile_entry(e, True) for e in outs) if x is not None]
        if not self.scope_in:
            raise PolicyError("%s has no enforceable in-scope web entry" % h)
        rps = rules.get("rate_limit_rps")
        if rps is None:
            self.rps = DEFAULT_RPS
            self.automation = True
        elif isinstance(rps, bool) or not isinstance(rps, (int, float)) or rps < 0:
            raise PolicyError("%s: rate_limit_rps %r is not a number >= 0" % (h, rps))
        elif rps == 0:
            self.rps = DEFAULT_RPS   # human traffic still gets the default limit
            self.automation = False
        else:
            self.rps = float(rps)
            self.automation = True
        if rules.get("automation") == "forbidden":
            self.automation = False
        self.ai_forbidden = rules.get("ai_use") == "forbidden"
        self.rules_rps = rps
        self.ident = Identification(rules, username)
        self.recon = compile_recon(recon)
        self.listed_networks = [e.network for e in self.scope_in if e.kind == "cidr"]
        self.out_networks = [e.network for e in self.scope_out if e.kind == "cidr"]

    # -- host level (CONNECT, server connect) -------------------------------------------
    def host_decision(self, host):
        """(reason or None, category) using only the host. category: target | recon-source."""
        h = norm_host(host)
        if not h:
            return "host-mismatch", "target"
        for e in self.scope_out:
            if entry_whole_host(e) and entry_matches_host(e, h, True):
                return "out-of-scope", "target"
        if h in self.recon["hosts"]:
            return None, "recon-source"
        for e in self.scope_in:
            if entry_matches_host(e, h, False):
                return None, "target"
        return "not-in-scope", "target"

    # -- request level (steps 2-4) -------------------------------------------------------
    def scope_decision(self, host, scheme, port, path):
        h = norm_host(host)
        for e in self.scope_out:
            if entry_matches(e, h, scheme, port, path, True):
                return "out-of-scope", "target"
        if h in self.recon["hosts"]:
            return None, "recon-source"
        for e in self.scope_in:
            if entry_matches(e, h, scheme, port, path, False):
                return None, "target"
        return "not-in-scope", "target"

    # -- step 5 ----------------------------------------------------------------------------
    def address_decision(self, ips):
        """Resolved addresses of an allowed host -> reason or None."""
        if not ips:
            return None
        for raw in ips:
            ip = parse_ip(raw)
            if ip is None:
                return "internal-address"
            if any(ip.version == n.version and ip in n for n in self.out_networks):
                return "out-of-scope"
            if is_internal(ip) and not any(ip.version == n.version and ip in n for n in self.listed_networks):
                return "internal-address"
        return None

    # -- step 6 (automation part) -----------------------------------------------------------
    def actor_decision(self, actor):
        if actor == "human":
            return None
        if not self.automation:
            return "automation-forbidden"
        if actor == "claude" and self.ai_forbidden:
            return "automation-forbidden"
        return None

    def describe(self):
        return {"program": self.handle, "rate_limit_rps": self.rules_rps, "effective_rps": self.rps,
                "automation": "allowed" if self.automation else "forbidden",
                "ai_use_forbidden": self.ai_forbidden, "identification": self.ident.describe(),
                "scope_in": len(self.scope_in), "scope_out": len(self.scope_out),
                "listed_networks": [str(n) for n in self.listed_networks],
                "recon_sources": len(self.recon["hosts"])}


# ---------------------------------------------------------------- host agreement and actors

def host_mismatch(authority, sni, host_header, tls):
    """CONTRACTS §3.1: CONNECT target (or absolute-URL authority), TLS SNI and Host must agree.
    Returns the agreed host, or None on mismatch. A TLS connection without SNI is accepted only
    when the target is an IP literal (clients send no SNI for IPs)."""
    a = norm_host(authority)
    hh = norm_host(split_host_port(host_header)[0]) if host_header else ""
    if not a or not hh or a != hh:
        return None
    if tls:
        s = norm_host(sni) if sni else ""
        if not s:
            return a if is_ip_literal(a) else None
        if s != a:
            return None
    return a


def actor_for(port_actor, header_value):
    """port_actor: the actor of the listener the client used ('human' on 8080, 'claude' on 8082).
    On the claude port the header cannot change the actor. Elsewhere a valid header wins; an
    invalid one counts as an unknown tool (never as human)."""
    if port_actor == "claude":
        return "claude"
    if header_value is None:
        return port_actor
    v = header_value.strip().lower()
    return v if ACTOR_RE.match(v) else "tool:unknown"


# ---------------------------------------------------------------- the decision (no I/O)

class Decision(object):
    __slots__ = ("allowed", "reason", "category", "host")

    def __init__(self, allowed, reason, category, host):
        self.allowed, self.reason, self.category, self.host = allowed, reason, category, host

    def __repr__(self):
        return "Decision(%s, %s, %s)" % ("allowed" if self.allowed else "refused", self.reason, self.category)


def decide(policy, authority, sni, host_header, tls, scheme, port, path, actor, ips=None):
    """Steps 0-6a of CONTRACTS §3.1 (everything except waiting for a rate-limit token).
    ips: resolved addresses of the host (None = not resolved, skip step 5)."""
    host = host_mismatch(authority, sni, host_header, tls)
    if host is None:
        return Decision(False, "host-mismatch", "target", norm_host(authority))
    if policy is None:
        return Decision(False, "no-profile", "target", host)
    reason, category = policy.scope_decision(host, scheme, port, path)
    if reason:
        return Decision(False, reason, category, host)
    if ips is not None:
        reason = policy.address_decision(ips)
        if reason:
            return Decision(False, reason, category, host)
    if category == "target":
        reason = policy.actor_decision(actor)
        if reason:
            return Decision(False, reason, category, host)
    return Decision(True, None, category, host)


def refusal_body(reason, detail=""):
    text = {
        "no-profile": "no active program profile (host/bounty program use <handle>)",
        "host-mismatch": "CONNECT target, TLS SNI and Host header disagree",
        "out-of-scope": "host or path is out of scope for the active program",
        "not-in-scope": "host or path is not in the active program's scope",
        "internal-address": "host resolves to an internal address the profile does not list",
        "rate-limited": "rate limit: no token within 10 s",
        "automation-forbidden": "the program does not allow automated tools or Claude",
    }.get(reason, reason)
    return "bounty-guard refused: %s%s\n" % (text, (" (" + detail + ")") if detail else "")


# ---------------------------------------------------------------- rate limit

class RateLimiter(object):
    """Strict spacing token bucket (capacity 1): request n may start at earliest
    previous_slot + 1/rps. FIFO: each caller reserves the next slot. A slot further away than
    max_wait is not reserved and the caller is refused (rate-limited)."""

    def __init__(self, rps, max_wait=MAX_WAIT_S):
        self.interval = 1.0 / float(rps)
        self.max_wait = max_wait
        self.next_slot = 0.0

    def reserve(self, now):
        """Seconds to wait before sending, or None when refused."""
        slot = max(now, self.next_slot)
        wait = slot - now
        if wait > self.max_wait:
            return None
        self.next_slot = slot + self.interval
        return wait
