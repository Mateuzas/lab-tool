"""Two-account access-control check: the pure logic (CONTRACTS §13). Stdlib only.

bounty-ac (in kali-bounty) uses it to pick and rebuild requests and judge the answers; the Mac's
unit tests import it by path, like guard_policy.py. Nothing here touches the network or disk.
"""

import difflib
import re
import urllib.parse

SAFE = ("GET", "HEAD", "OPTIONS")
STRONG_COOKIE = 16          # a cookie this long is a session credential, not a preference
SIMILAR = 0.95
COMPARE_BYTES = 64 * 1024
HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "content-length", "accept-encoding"}
NEVER_SESSION = {"host", "content-length", "content-type", "accept", "user-agent"}
STATIC = re.compile(r"\.(js|mjs|css|map|png|jpe?g|gif|svg|ico|webp|avif|woff2?|ttf|otf|eot|mp4|webm|mp3)$", re.I)
SECRET_KEY = re.compile(r"(token|key|secret|sig|signature|session|sess|auth|code|password|pass|jwt|ticket)", re.I)
_HEADER = re.compile(r"^([A-Za-z0-9!#$%&'*+.^_`|~-]+):[ \t]*(.*?)[ \t]*$")
_IDS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
                  r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                  r"|(?<![0-9])[0-9]{6,}(?![0-9])")
FLAGGED = ("leak", "same-as-a", "a-data")


class SessionError(ValueError):
    pass


def parse_session(text):
    """`Name: value` lines -> [[name, value], ...]; blank and # lines ignored."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _HEADER.match(line)
        if not m or not m.group(2):
            raise SessionError("not a 'Name: value' header line")
        name = m.group(1)
        if name.lower() in NEVER_SESSION or name.lower() in HOP:
            raise SessionError("%s can't be a session header" % name)
        out.append([name, m.group(2)])
    if not out:
        raise SessionError("no 'Name: value' header lines on stdin")
    return out


def cookie_pairs(value):
    out = {}
    for part in (value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _get_all(headers, name):
    n = name.lower()
    return [v for k, v in headers if k.lower() == n]


def matches(session, headers):
    """True when the request headers carry this session (CONTRACTS §13.2)."""
    req_cookies = {}
    for v in _get_all(headers, "cookie"):
        req_cookies.update(cookie_pairs(v))
    for name, value in session or []:
        if name.lower() == "cookie":
            for k, v in cookie_pairs(value).items():
                if len(v) >= STRONG_COOKIE and req_cookies.get(k) == v:
                    return True
        elif value in _get_all(headers, name):
            return True
    return False


def session_names(*sessions):
    return {name.lower() for s in sessions for name, _ in (s or [])}


def rewrite(headers, sessions, use):
    """Headers for a replay: hop-by-hop and every session header name removed, then `use`'s
    session headers added (use None = no session). Accept-Encoding is set to identity so the
    answer can be compared as text."""
    drop = HOP | session_names(*sessions)
    out = [[k, v] for k, v in headers if k.lower() not in drop and k.lower() != "x-bounty-actor"]
    for name, value in use or []:
        out.append([name, value])
    out.append(["Accept-Encoding", "identity"])
    return out


def is_safe(method):
    return (method or "").upper() in SAFE


def is_static(url):
    return bool(STATIC.search(urllib.parse.urlsplit(url or "").path))


def redact_url(url):
    """Query values of token-like keys -> ***."""
    p = urllib.parse.urlsplit(url or "")
    if not p.query:
        return url
    q = [(k, "***" if SECRET_KEY.search(k) else v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q, safe="*"), p.fragment))


def _text(body):
    if isinstance(body, bytes):
        body = body[:COMPARE_BYTES].decode("utf-8", "replace")
    return (body or "")[:COMPARE_BYTES]


def similarity(a, b):
    a, b = _text(a), _text(b)
    if not a and not b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 3)


def ids_in(text):
    return set(_IDS.findall(_text(text)))


def a_ids(a_body, url, body):
    """A's identifiers (emails, UUIDs, numbers of ≥ 6 digits) that are not in the URL and do appear in `body`."""
    in_url = urllib.parse.unquote(url or "")
    mine = {i for i in ids_in(a_body) if i not in in_url}
    found = ids_in(body)
    return len(mine & found)


def marker_hits(markers, body):
    t = _text(body)
    return sum(1 for m in markers or [] if m and m in t)


def verdict(a_status, status, sim, markers, aids, refused=None):
    """CONTRACTS §13.3."""
    if refused:
        return "refused"
    if status is None:
        return "error"
    if status in (401, 403, 404, 405) or 300 <= status < 400:
        return "blocked"
    if not (200 <= status < 300) and a_status is not None and 200 <= a_status < 300:
        return "blocked"
    if markers:
        return "leak"
    if sim >= SIMILAR:
        return "same-as-a"
    if aids:
        return "a-data"
    return "differs"


def hint(b_verdict, none_verdict):
    b, n = b_verdict in FLAGGED, none_verdict in FLAGGED
    if b and n:
        return "public"
    if b:
        return "likely-broken-access"
    return "none"


def judge(a_status, a_body, url, markers, status, body, refused=None):
    """One variant -> the §13.3 record (no body kept)."""
    sim = similarity(a_body, body) if status is not None and not refused else 0.0
    m = marker_hits(markers, body) if status is not None and not refused else 0
    ids = a_ids(a_body, url, body) if status is not None and not refused else 0
    rec = {"status": status, "length": len(body or b""), "similarity": sim, "markers": m, "a_ids": ids,
           "verdict": verdict(a_status, status, sim, m, ids, refused)}
    if refused:
        rec["refused"] = refused
    return rec
