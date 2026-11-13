"""Intigriti Researcher API client (ADR 009, docs/research/intigriti-api.md).

Only api.intigriti.com is ever contacted: the base URL is fixed, every request URL is checked, and
redirects are refused (they would also carry the Authorization header elsewhere). The token is held
in memory only and never appears in errors: messages carry the method, path and status, never headers.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from bounty.util import BountyError

API_HOST = "api.intigriti.com"
BASE_URL = "https://api.intigriti.com/external/researcher"
USER_AGENT = "kali-lab-bounty/0.1"
MAX_RETRIES = 3
MIN_INTERVAL = 1.0  # seconds between requests (≤ 1 request/second)
TIMEOUT = 60


class Response(object):
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body  # bytes, exactly as received
        self.headers = headers or {}

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BountyError("parse", "response is not JSON (status %d)" % self.status, 4)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


def _urllib_transport(method, url, headers):
    """Default transport. Returns Response for any HTTP status; raises OSError on network failure."""
    req = urllib.request.Request(url, method=method, headers=headers)
    # No proxy handler from the environment: the token goes straight to api.intigriti.com over TLS.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return Response(r.status, r.read(), dict(r.headers.items()))
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return Response(e.code, body, dict(e.headers.items()) if e.headers else {})


class Client(object):
    def __init__(self, token, transport=None, sleep=time.sleep, clock=time.monotonic, log=None):
        if not token:
            raise BountyError("auth", "INTIGRITI_TOKEN not set "
                              "(run via scripts/secrets.sh exec intigriti -- host/bounty …)", 2)
        self._token = token
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._last = None
        self._log = log or (lambda msg: None)
        self.requests = 0

    def _url(self, path, params=None):
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != API_HOST:
            raise BountyError("internal", "refusing to contact %s" % parts.hostname, 3)
        return url

    def _throttle(self):
        if self._last is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def get(self, path, params=None):
        """GET with ≤ 1 req/s and up to MAX_RETRIES retries on 429/5xx/network errors.

        Returns the final Response (any status except 401). 401 raises BountyError(exit 2).
        Network failure after retries raises BountyError(exit 3).
        """
        url = self._url(path, params)
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        shown = path + ("?" + urllib.parse.urlencode(params) if params else "")
        attempt = 0
        while True:
            self._throttle()
            self.requests += 1
            try:
                resp = self._transport("GET", url, headers)
            except (OSError, ValueError) as e:  # URLError, timeouts, TLS errors
                resp = None
                problem = "network error (%s)" % type(e).__name__
            else:
                problem = None
                if resp.status == 401:
                    raise BountyError("auth", "Intigriti rejected the token (401 on GET %s)" % path, 2)
                if resp.status == 429 or resp.status >= 500:
                    problem = "HTTP %d" % resp.status
            if problem is None:
                return resp
            if attempt >= MAX_RETRIES:
                raise BountyError("api", "GET %s failed after %d retries: %s" % (shown, MAX_RETRIES, problem), 3)
            delay = 2.0 ** (attempt + 1)
            if resp is not None:
                ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                try:
                    delay = min(60.0, max(delay, float(ra)))
                except (TypeError, ValueError):
                    pass
            self._log("GET %s: %s, retry in %.0fs" % (shown, problem, delay))
            self._sleep(delay)
            attempt += 1

    def get_json(self, path, params=None):
        resp = self.get(path, params)
        if resp.status != 200:
            raise BountyError("api", "GET %s returned HTTP %d" % (path, resp.status), 3)
        return resp, resp.json()

    # Endpoints ------------------------------------------------------------------------------

    def programs_page(self, limit, offset):
        return self.get_json("/v1/programs", {"limit": limit, "offset": offset})

    def program_detail(self, program_id):
        """Returns Response (200, 403 or other final status); caller decides."""
        safe = urllib.parse.quote(str(program_id), safe="")
        return self.get("/v1/programs/" + safe)

    def activities_page(self, limit, offset):
        return self.get_json("/v1/programs/activities", {"limit": limit, "offset": offset})
