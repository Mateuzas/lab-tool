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


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/ip":
            body = b"198.51.100.20\n"
            ctype = "text/plain"
        else:
            info = {"method": self.command, "path": self.path, "host": self.headers.get("Host"),
                    "tls": isinstance(self.connection, ssl.SSLSocket),
                    "headers": {k: v for k, v in self.headers.items()}}
            with lock, open(LOG, "a") as f:
                f.write(json.dumps(info) + "\n")
            body = (json.dumps(info) + "\n").encode()
            ctype = "application/json"
        self.send_response(200)
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
