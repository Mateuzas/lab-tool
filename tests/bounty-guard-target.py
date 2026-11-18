"""Local test target for tests/bounty-guard-test.sh (never a real host).

Runs inside the throwaway target container: HTTP on :80 and HTTPS on :443 (certificate from the
test CA, SANs for the fake .test names). Every request is answered with JSON describing what
arrived (method, path, Host, headers), so the test can see exactly what the guard forwarded.
/ip answers with a fixed documentation address (the stubbed egress-IP lookup).
Usage: python3 bounty-guard-target.py <certfile> <keyfile> <logfile>
"""

import http.server
import json
import ssl
import sys
import threading

CERT, KEY, LOG = sys.argv[1], sys.argv[2], sys.argv[3]
lock = threading.Lock()


EXPOSED = {"/.git/config": b"[core]\n\trepositoryformatversion = 0\n",
           "/.env": b"APP_ENV=prod\nDB_PASSWORD=FAKE_GUARD_TEST_VALUE\n"}

# Access-control endpoints for the BE-19 check (CONTRACTS §13): sid A = alice, sid B = bob.
AC_A, AC_B = "a" * 20, "b" * 20
ALICE = {"owner": "alice@acme.test", "account": "1000001", "doc": "alice's travel plans for spring"}
BOB = {"owner": "bob@acme.test", "account": "2000002", "doc": "bob keeps different notes here"}


def ac_answer(method, path, cookie):
    sid = "a" if "sid=" + AC_A in cookie else ("b" if "sid=" + AC_B in cookie else None)
    route = path.split("?")[0]
    if route == "/ac/public":
        return 200, json.dumps({"news": "a public post", "id": "7777777"}).encode()
    if sid is None:
        return 401, b'{"error":"log in"}'
    if route == "/ac/idor":                      # any logged-in user gets alice's document
        return 200, json.dumps(ALICE).encode()
    if route == "/ac/protected":
        return (200, json.dumps(ALICE).encode()) if sid == "a" else (403, b'{"error":"not yours"}')
    if route == "/ac/own":
        return 200, json.dumps(ALICE if sid == "a" else BOB).encode()
    if route in ("/ac/write", "/ac/app.js"):
        return 200, json.dumps(ALICE).encode()
    return 404, b"{}"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        status = 200
        if self.path == "/ip":
            body = b"198.51.100.20\n"
            ctype = "text/plain"
        elif self.path in EXPOSED:              # planted for the BE-20 exposure checks (CONTRACTS §10.6)
            body = EXPOSED[self.path]
            ctype = "text/plain"
        elif self.path.startswith("/ac/"):
            status, body = ac_answer(self.command, self.path, self.headers.get("Cookie") or "")
            ctype = "application/json"
            with lock, open(LOG, "a") as f:     # so the test can see which session each replay carried
                f.write(json.dumps({"method": self.command, "path": self.path,
                                    "headers": {k: v for k, v in self.headers.items()}}) + "\n")
        else:
            info = {"method": self.command, "path": self.path, "host": self.headers.get("Host"),
                    "tls": isinstance(self.connection, ssl.SSLSocket),
                    "headers": {k: v for k, v in self.headers.items()}}
            with lock, open(LOG, "a") as f:
                f.write(json.dumps(info) + "\n")
            body = (json.dumps(info) + "\n").encode()
            ctype = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_HEAD = do_PUT = _answer

    def log_message(self, *a):
        pass


def serve(port, tls):
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()


threading.Thread(target=serve, args=(80, False), daemon=True).start()
serve(443, True)
