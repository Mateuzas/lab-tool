"""Two-account access-control check: the pure logic (CONTRACTS §13). Stdlib only.

bounty-ac (in kali-bounty) uses it to pick and rebuild requests and judge the answers; the Mac's
unit tests import it by path, like guard_policy.py. Nothing here touches the network or disk.
"""

import difflib
import json
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
FLAGGED = ("leak", "same-as-a", "a-data", "wrote")
WRITES = ("POST", "PUT", "PATCH", "DELETE")
TINY = 8                    # an answer this short (stripped) says nothing about whose data it is
REPRESENTATION = ("return=representation", "count=exact")
# PostgREST's Content-Range has no unit ("0-24/100", "*/0"); an HTTP byte range says "bytes ...".
_PGRST_RANGE = re.compile(r"^\s*(\*|[0-9]+-[0-9]+)/(\*|[0-9]+)\s*$")


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


def is_postgrest(url, a_resp_headers):
    """True when the request goes to a PostgREST table (CONTRACTS §13.3): the path has `/rest/v1/`
    (Supabase's mount), or A's recorded answer has `Server: postgrest...` or a unit-less
    PostgREST `Content-Range` (`0-0/*`, `*/3`). RPC calls (`/rpc/` right after the API root) are
    not tables: their body is the function's return value, not the written rows, so they're excluded."""
    path = urllib.parse.urlsplit(url or "").path
    if re.search(r"/rest/v1/rpc/", path) or re.search(r"/rpc/[^/]+/?$", path):
        return False
    if "/rest/v1/" in path:
        return True
    if any("postgrest" in v.lower() for v in _get_all(a_resp_headers or [], "server")):
        return True
    return any(_PGRST_RANGE.match(v) for v in _get_all(a_resp_headers or [], "content-range"))


def counts_rows(method, url, a_resp_headers):
    """A write to a PostgREST table: replay it asking for the changed rows back."""
    return (method or "").upper() in WRITES and is_postgrest(url, a_resp_headers)


def with_representation(headers):
    """One Prefer header: the request's own preferences kept, any return=/count= replaced by
    return=representation and count=exact. Only what comes back changes, not which rows."""
    keep, at = [], None
    out = []
    for k, v in headers:
        if k.lower() == "prefer":
            if at is None:
                at = len(out)
            for tok in v.split(","):
                t = tok.strip()
                if t and t.split("=", 1)[0].strip().lower() not in ("return", "count") and t not in keep:
                    keep.append(t)
            continue
        out.append([k, v])
    value = ", ".join(keep + list(REPRESENTATION))
    out.insert(len(out) if at is None else at, ["Prefer", value])
    return out


def count_rows(body, content_range=None):
    """Rows in a return=representation answer: a JSON array's length (an object = 1), else the
    total from Content-Range (`*/N`); None when it can't be told."""
    try:
        data = json.loads(_text(body)) if (body or b"").strip() else None
    except ValueError:
        data = None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return 1
    m = _PGRST_RANGE.match(content_range or "")
    if m and m.group(2) != "*":
        return int(m.group(2))
    return None


def tiny(body):
    return len(_text(body).strip()) <= TINY


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


def verdict(a_status, status, sim, markers, aids, refused=None, rows=None, empty=False):
    """CONTRACTS §13.3. `rows`: rows a PostgREST write changed (None = not counted); `empty`:
    A's answer and this one are both ≤ TINY bytes."""
    if refused:
        return "refused"
    if status is None:
        return "error"
    if status in (401, 403, 404, 405) or 300 <= status < 400:
        return "blocked"
    if not (200 <= status < 300) and a_status is not None and 200 <= a_status < 300:
        return "blocked"
    if rows is not None:
        return "wrote" if rows > 0 else "no-effect"
    if markers:
        return "leak"
    if empty:
        return "inconclusive"
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


def judge(a_status, a_body, url, markers, status, body, refused=None, count=False, content_range=None):
    """One variant -> the §13.3 record (no body kept). `count`: this was a PostgREST write replayed
    with Prefer: return=representation, so the answer lists the rows it changed."""
    got = status is not None and not refused
    sim = similarity(a_body, body) if got else 0.0
    m = marker_hits(markers, body) if got else 0
    ids = a_ids(a_body, url, body) if got else 0
    rows = count_rows(body, content_range) if got and count and 200 <= status < 300 else None
    empty = got and tiny(a_body) and tiny(body)
    v = verdict(a_status, status, sim, m, ids, refused, rows, empty)
    rec = {"status": status, "length": len(body or b""), "similarity": sim, "markers": m, "a_ids": ids, "verdict": v}
    if rows is not None:
        rec["rows"] = rows
    if v == "inconclusive":
        rec["reason"] = ("both answers are empty" if not _text(a_body).strip() and not _text(body).strip()
                         else "both answers are %d bytes or less" % TINY)
    if refused:
        rec["refused"] = refused
    return rec
