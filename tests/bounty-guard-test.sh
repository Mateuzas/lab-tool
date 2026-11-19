#!/usr/bin/env bash
# Proves the Bug bounty scope guard (BE-5, ADR 005, CONTRACTS §3, §5, §6) without any real host.
#
#   tests/bounty-guard-test.sh            builds kali-lab:guard-test from container/ and runs
#   BOUNTY_TEST_NO_BUILD=1 tests/...      reuse the image already built
#
# Everything is local and thrown away afterwards: two --internal Docker networks (203.0.113.0/24
# and 10.99.0.0/24: no route to the internet), a target container with HTTP + HTTPS echo servers
# (tests/bounty-guard-target.py, certificate from a throwaway test CA), fake *.test hostnames in the
# guard container's /etc/hosts, fake program profiles in a temporary workspace. The guard container
# is started with host/bounty guard start under another name and port, so kali-bounty, kali-lab
# and 127.0.0.1:8081 are never touched. The egress-IP lookup is stubbed to the target.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD

IMAGE=${BOUNTY_TEST_IMAGE:-kali-lab:guard-test}
C=kali-bounty-test
T=kali-bounty-test-target
NET=kali-bounty-testnet
NETI=kali-bounty-testnet-int
PORT=18081
# Docker Desktop (macOS) maps bind-mount ownership to the Mac user, so the workspace this script
# writes and hunter (uid 1000 in the image) writes look like one owner. Docker on Linux keeps real
# uids: the suite only works there when it runs as uid 1000 (CI does: .github/workflows/ci.yml).
if [ "$(uname -s)" = Linux ] && [ "$(id -u)" != 1000 ]; then
  echo "on Linux run this as uid 1000 (the image's hunter), not uid $(id -u): hunter must own the bind-mounted workspace" >&2
  exit 2
fi
mkdir -p "$ROOT/workspace"
WS=$(mktemp -d "$ROOT/workspace/.guard-test.XXXXXX")
VOL=kali-bounty-test-guard
VAR=/var/lib/bounty-guard
export BOUNTY_CONTAINER=$C BOUNTY_LAB_CONTAINER=kali-lab-guard-test-none BOUNTY_IMAGE=$IMAGE \
       BOUNTY_GUARD_PORT=$PORT BOUNTY_WORKSPACE=$WS BOUNTY_GUARD_VOLUME=$VOL BOUNTY_HOME_VOLUME=$VOL-home

fail=0
check() { if [ "$1" = ok ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }
expect() { if [ "$1" = "$2" ]; then check ok "$3"; else check bad "$3 (got '$1', want '$2')"; fi; }

cleanup() {
  docker rm -f "$C" "$T" >/dev/null 2>&1
  docker network rm "$NET" "$NETI" >/dev/null 2>&1
  docker volume rm "$VOL" "$VOL-home" >/dev/null 2>&1
  rm -rf "$WS"
}
[ -n "${BOUNTY_TEST_KEEP:-}" ] || trap cleanup EXIT
cleanup_quiet() { docker rm -f "$C" "$T" >/dev/null 2>&1; docker network rm "$NET" "$NETI" >/dev/null 2>&1; docker volume rm "$VOL" "$VOL-home" >/dev/null 2>&1; }
cleanup_quiet

as_root() { docker exec "$C" "$@"; }
# curl through the guard as hunter: prints "<status> <reason>" (reason from X-Bounty-Guard)
req() {
  local actor=$1; shift
  docker exec "$C" bounty-run --actor "$actor" -- curl -s -o /dev/null -m 20 \
    -w '%{http_code} %header{x-bounty-guard}' "$@" 2>/dev/null | sed 's/refused; reason=//'
}
body() { local actor=$1; shift; docker exec "$C" bounty-run --actor "$actor" -- curl -s -m 20 "$@" 2>/dev/null; }
flows() { as_root sh -c "cat $VAR/flows/$1/*.jsonl 2>/dev/null"; }
use() { INTIGRITI_USERNAME=tester host/bounty program use "$1" >/dev/null 2>&1; }
jget() { python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(eval(sys.argv[1]))' "$1"; }

echo "== setup (local only)"
if [ -z "${BOUNTY_TEST_NO_BUILD:-}" ]; then
  docker build -q -t "$IMAGE" container/ >/dev/null || { echo "image build failed"; exit 1; }
fi
docker network create --internal --subnet 203.0.113.0/24 "$NET" >/dev/null
docker network create --internal --subnet 10.99.0.0/24 "$NETI" >/dev/null

mkdir -p "$WS/testca"
docker run --rm --network none -v "$WS/testca":/w "$IMAGE" sh -c '
  cd /w
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=guard-test CA" -keyout ca.key -out ca.pem \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=app.acme.test" -keyout leaf.key -out leaf.csr 2>/dev/null
  printf "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\nsubjectAltName=DNS:app.acme.test,DNS:deep.app.acme.test,DNS:acme.test,DNS:community.acme.test,DNS:legacy.acme.test,DNS:other.test,DNS:whoami.recon.test,DNS:intranet.acme.test,DNS:meta.acme.test,IP:10.99.0.10,IP:203.0.113.10\n" > ext
  openssl x509 -req -in leaf.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 2 -extfile ext -out leaf.pem 2>/dev/null
  chmod 644 *' || { echo "test CA failed"; exit 1; }

docker run -d --name "$T" --network "$NET" --ip 203.0.113.10 \
  -v "$WS/testca":/w:ro -v "$ROOT/tests/bounty-guard-target.py":/srv/target.py:ro \
  "$IMAGE" python3 /srv/target.py /w/leaf.pem /w/leaf.key /tmp/requests.log >/dev/null
docker network connect --ip 10.99.0.10 "$NETI" "$T"

# Fake programs, shaped like the real Dropbox profile: wildcard, out-of-scope host inside it, a url
# entry without scheme, an "other" entry holding a hostname, identification templates.
python3 - "$WS" <<'PY'
import json, os, sys
ws = sys.argv[1]
def prog(handle, ins, outs, rps=2, ua="Intigriti - <username> - <user agent>",
         hdr="X-Intigriti-Username: {Username}", extra_rules=None):
    rules = {"version": "1", "changed_at": None, "safe_harbour": True, "rate_limit_rps": rps,
             "automation": "forbidden" if rps == 0 else ("unknown" if rps is None else "allowed"),
             "intigriti_me": True, "user_agent": ua, "request_header": hdr, "ai_use": "allowed-disclosed",
             "text": "test"}
    rules.update(extra_rules or {})
    p = {"schema": 1, "handle": handle, "platform": "intigriti", "platform_id": handle, "name": handle,
         "url": "https://app.intigriti.com/x", "status": "open", "confidentiality": "public",
         "industry": "Test", "detail": "ok", "synced_at": "2026-11-14T00:00:00Z", "first_seen_at": None,
         "bounty": {"min": 1, "max": 2, "currency": "EUR", "min_eur": 1, "max_eur": 2},
         "scope": {"version": "1", "changed_at": None,
                   "in": [dict(type=t, value=v, tier="tier-1", description="") for t, v in ins],
                   "out": [dict(type=t, value=v, description="") for t, v in outs]},
         "rules": rules, "legal_basis": "program"}
    d = os.path.join(ws, "programs", handle)
    os.makedirs(d, exist_ok=True)
    json.dump(p, open(os.path.join(d, "program.json"), "w"), indent=1)
ins = [("wildcard", "*.acme.test"), ("domain", "acme.test"), ("url", "other.test/allowed"),
       ("android", "com.acme.app"), ("other", "Acme Desktop")]
outs = [("domain", "community.acme.test"), ("url", "https://app.acme.test/admin"),
        ("other", "legacy.acme.test"), ("other", "Acme Passwords")]
prog("test-acme", ins, outs)
prog("test-acme-int", ins + [("cidr", "10.99.0.0/24")], outs)
prog("test-acme-norate", ins, outs, rps=0)
prog("test-acme-badtpl", ins, outs, hdr="X-Intigriti-Username: {Handle}")
os.makedirs(os.path.join(ws, "config"), exist_ok=True)
json.dump({"hosts": ["whoami.recon.test"], "rate_limit_rps": 2,
           "egress_ip_url": "https://whoami.recon.test/ip"},
          open(os.path.join(ws, "config", "recon-sources.json"), "w"))
PY
mkdir -p "$WS/programs/test-broken" && echo '{"schema": 1, "handle": "test-broken", "scope": ' > "$WS/programs/test-broken/program.json"

# The root filesystem is read-only, so the fake names go into /etc/hosts when the container is made.
BOUNTY_GUARD_ADD_HOSTS=""
for n in app.acme.test deep.app.acme.test acme.test community.acme.test legacy.acme.test other.test unrelated.test whoami.recon.test; do
  BOUNTY_GUARD_ADD_HOSTS="$BOUNTY_GUARD_ADD_HOSTS $n:203.0.113.10"
done
export BOUNTY_GUARD_ADD_HOSTS="$BOUNTY_GUARD_ADD_HOSTS intranet.acme.test:10.99.0.10 meta.acme.test:169.254.169.254"
host/bounty guard start || { echo "guard start failed"; docker logs "$C" | tail; exit 1; }
docker network connect --ip 203.0.113.2 "$NET" "$C"
docker network connect --ip 10.99.0.2 "$NETI" "$C"
as_root sh -c 'cp /bounty/testca/ca.pem /usr/local/share/ca-certificates/guard-test-ca.crt && update-ca-certificates >/dev/null 2>&1'
# Restart the guard so it reads the trust store with the test CA (upstream verification stays on).
as_root pkill -9 -u guard; sleep 3
for _ in $(seq 1 20); do [ "$(as_root cat $VAR/guard.json | jget 'd.get("guard")')" = up ] && break; sleep 0.5; done

echo "== (a) no profile: everything refused"
expect "$(req human http://app.acme.test/a)" "403 no-profile" "http to an in-scope-to-be host refused: no-profile"
expect "$(req human https://app.acme.test/a)" "403 no-profile" "https refused: no-profile"
expect "$(req claude https://unrelated.test/a)" "403 no-profile" "claude refused: no-profile"

echo "== program use"
host/bounty program use test-acme-badtpl >/dev/null 2>&1; rc=$?
expect "$rc" 4 "program use with an unknown placeholder exits 4"
host/bounty program use test-broken >/dev/null 2>&1; rc=$?
expect "$rc" 4 "program use with a profile that does not parse exits 4"
env -u INTIGRITI_USERNAME BOUNTY_WORKSPACE="$WS" host/bounty program use test-acme >/dev/null 2>&1; rc=$?
expect "$rc" 2 "program use without INTIGRITI_USERNAME (template needs it) exits 2"
INTIGRITI_USERNAME=tester host/bounty program use test-acme; rc=$?
expect "$rc" 0 "program use test-acme"
state() { as_root cat $VAR/guard.json | jget "$1"; }
wait_state() { for _ in $(seq 1 20); do [ "$(state "$1")" = "$2" ] && return 0; sleep 0.5; done; return 1; }
# Poll guard.json until the expression has the expected value (a read can come back empty while the
# container is busy), then check it once more: the assertion is exact, only the timing is tolerant.
expect_state() { wait_state "$1" "$2"; expect "$(state "$1")" "$2" "$3"; }
wait_state 'd["status"]' active
expect "$(state '(d["status"], d["program"], d["effective_rps"])')" "('active', 'test-acme', 2.0)" "guard.json: active, program, rate"
for _ in $(seq 1 20); do eg=$(as_root cat $VAR/guard.json | jget 'd.get("egress_ip")'); [ "$eg" != None ] && break; sleep 0.5; done
expect "$eg" "198.51.100.20" "egress IP looked up through the (stubbed) recon source"
[ "$eg" = "198.51.100.20" ] || echo "  egress_error: $(state 'd.get("egress_error")')"

echo "== (b) in scope allowed"
expect "$(req human http://app.acme.test/b1)" "200 " "http://app.acme.test (wildcard)"
expect "$(req human https://app.acme.test/b2)" "200 " "https://app.acme.test (TLS intercepted, upstream verified)"
expect "$(req human https://deep.app.acme.test/b3)" "200 " "wildcard matches any depth"
expect "$(req human https://acme.test/b4)" "200 " "apex allowed by its own domain entry"
expect "$(req human https://other.test/allowed/b5)" "200 " "url entry without scheme: path prefix allowed"

echo "== (c) out of scope beats in scope"
expect "$(req human https://community.acme.test/c1)" "403 out-of-scope" "community.acme.test inside *.acme.test"
expect "$(req human https://app.acme.test/admin/c2)" "403 out-of-scope" "out-of-scope url path /admin"
expect "$(req human 'https://app.acme.test/x/..%2fadmin/c3')" "403 out-of-scope" "encoded ../ into /admin"
expect "$(req human https://legacy.acme.test/c4)" "403 out-of-scope" "out-of-scope 'other' entry that is a hostname"

echo "== (d) not in scope"
expect "$(req human https://unrelated.test/d1)" "403 not-in-scope" "unrelated host"
expect "$(req human https://other.test/elsewhere/d2)" "403 not-in-scope" "url entry: path outside the prefix"
expect "$(req human https://other.test/allowedx/d3)" "403 not-in-scope" "url entry: prefix only on segment boundary"
expect "$(req human http://203.0.113.10/d4)" "403 not-in-scope" "IP literal not listed"

echo "== (e) CONNECT / SNI / Host must agree"
expect "$(req human https://app.acme.test/e1 -H 'Host: community.acme.test')" "403 host-mismatch" "Host header differs from CONNECT+SNI"
expect "$(req human http://app.acme.test/e2 -H 'Host: unrelated.test')" "403 host-mismatch" "plain http: Host differs from the URL"
sni=$(docker exec "$C" bounty-run --actor human -- python3 -c '
import socket, ssl
s = socket.create_connection(("127.0.0.1", 8080), timeout=10)
s.sendall(b"CONNECT app.acme.test:443 HTTP/1.1\r\nHost: app.acme.test:443\r\n\r\n")
assert b" 200" in s.recv(4096)
t = ssl.create_default_context().wrap_socket(s, server_hostname="deep.app.acme.test")
t.sendall(b"GET /e3 HTTP/1.1\r\nHost: deep.app.acme.test\r\nConnection: close\r\n\r\n")
data = b""
while True:
    c = t.recv(4096)
    if not c: break
    data += c
head = data.split(b"\r\n\r\n")[0].decode()
print(head.split()[1], [l.split("reason=")[1] for l in head.split("\r\n") if "reason=" in l][0] if "reason=" in head else "")
' 2>&1 | tail -1)
expect "$sni" "403 host-mismatch" "CONNECT target differs from SNI+Host"

echo "== (f) internal addresses"
expect "$(req human https://intranet.acme.test/f1)" "403 internal-address" "in-scope name resolving to 10.99.0.10 refused"
expect "$(req human http://meta.acme.test/f2)" "403 internal-address" "in-scope name resolving to 169.254.169.254 refused"
use test-acme-int
expect "$(req human https://intranet.acme.test/f3)" "200 " "allowed once the profile lists 10.99.0.0/24"
expect "$(req human https://10.99.0.10/f4)" "200 " "listed CIDR also allows the IP literal"
use test-acme

echo "== (g) nothing leaves except through the guard"
out=$(docker exec "$C" bounty-run --actor human -- curl -s -m 5 --noproxy '*' -o /dev/null -w '%{http_code}' http://203.0.113.10/g1 2>&1)
expect "$out" "000" "curl --noproxy as hunter cannot connect (203.0.113.10 via eth1)"
out=$(docker exec "$C" bounty-run --actor human -- bash -c 'timeout 5 bash -c "exec 3<>/dev/tcp/203.0.113.10/443" 2>/dev/null && echo open || echo blocked')
expect "$out" "blocked" "raw TCP connect as hunter blocked"
out=$(docker exec "$C" bash -c 'timeout 5 bash -c "exec 3<>/dev/tcp/203.0.113.10/80" 2>/dev/null && echo open || echo blocked')
expect "$out" "blocked" "raw TCP connect as root blocked too (only uid guard)"
out=$(docker exec "$C" bounty-run --actor human -- python3 -c '
import socket
ns = [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")][0]
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.sendto(b"\x00", (ns, 53)); print("sent")
except OSError: print("blocked")')
expect "$out" "blocked" "DNS to the resolver as hunter blocked (only the guard resolves)"
out=$(docker exec "$C" bounty-run --actor human -- python3 -c '
import socket
s = socket.create_connection(("127.0.0.1", 8080), timeout=10)
s.sendall(b"CONNECT app.acme.test:80 HTTP/1.1\r\nHost: app.acme.test:80\r\n\r\n")
s.recv(4096)
s.sendall(b"\x16\x00\x00rawbytes\r\n\r\n")
s.settimeout(5)
d = b""
try:
    while True:
        c = s.recv(4096)
        if not c: break
        d += c
except Exception: pass
# The target is a Python http.server: anything it answered carries "Server: BaseHTTP".
print("relayed" if b"BaseHTTP" in d else "not-relayed")')
expect "$out" "not-relayed" "raw (non-HTTP) bytes through a CONNECT tunnel are not relayed"
out=$(as_root ip6tables -S OUTPUT | head -1)
expect "$out" "-P OUTPUT DROP" "IPv6 OUTPUT policy DROP"

echo "== (i) identification added, actor header stripped"
echo_json=$(body claude https://app.acme.test/i1 -A 'TestAgent/1.0' -H 'X-Intigriti-Username: spoofed')
expect "$(echo "$echo_json" | jget '{k.lower(): v for k, v in d["headers"].items()}.get("x-intigriti-username")')" "tester" "X-Intigriti-Username replaced with the configured username"
expect "$(echo "$echo_json" | jget '{k.lower(): v for k, v in d["headers"].items()}.get("user-agent")')" "Intigriti - tester - TestAgent/1.0" "User-Agent rewritten from the template"
expect "$(echo "$echo_json" | jget '[k for k in d["headers"] if k.lower()=="x-bounty-actor"]')" "[]" "X-Bounty-Actor not forwarded"
echo_json=$(body human http://app.acme.test/i2 -H 'User-Agent:' -H 'X-Bounty-Actor: tool:mytool')
expect "$(echo "$echo_json" | jget '{k.lower(): v for k, v in d["headers"].items()}.get("user-agent")')" "Intigriti - tester - kali-lab" "missing User-Agent becomes kali-lab"
expect "$(echo "$echo_json" | jget '[k for k in d["headers"] if k.lower()=="x-bounty-actor"]')" "[]" "X-Bounty-Actor stripped on plain http too"

echo "== keep-alive: many requests on one client connection (upstream connections don't pile up)"
# Before the fix every request left an idle upstream connection open; the fake target (like servers
# that cap connections) stopped answering after 5 and every 6th request hung.
got=$(docker exec "$C" bounty-run --actor human -- sh -c 'u=""; for i in $(seq 1 12); do u="$u -o /dev/null https://app.acme.test/ka-$i"; done; curl -s --http1.1 -m 15 -w "%{http_code} " $u')
expect "$(echo $got)" "200 200 200 200 200 200 200 200 200 200 200 200" "12 keep-alive HTTP/1.1 requests through the guard all answered"

echo "== tools through bounty-run (proxy flag + actor header injected)"
as_root sh -c 'printf "t1\nt2\n" > /home/hunter/words && chown hunter /home/hunter/words'
docker exec "$C" bounty-run --actor human -- ffuf -s -u https://app.acme.test/ffuf-FUZZ -w /home/hunter/words >/dev/null 2>&1
docker exec "$C" bounty-run --actor human -- httpx-toolkit -silent -duc -u https://app.acme.test/httpx-probe >/dev/null 2>&1
docker exec "$C" bounty-run --actor human -- katana -silent -duc -d 1 -u https://app.acme.test/katana-start >/dev/null 2>&1
as_root sh -c 'cat > /home/hunter/t.yaml <<EOF
id: guard-test
info: {name: guard test, author: test, severity: info}
http:
  - method: GET
    path: ["{{BaseURL}}/nuclei-probe"]
    matchers: [{type: status, status: [200]}]
EOF
chown hunter /home/hunter/t.yaml'
docker exec "$C" bounty-run --actor human -- nuclei -silent -duc -ni -t /home/hunter/t.yaml -u https://app.acme.test >/dev/null 2>&1
sleep 1
fl=$(flows test-acme)
for t in ffuf:ffuf-t1 httpx-toolkit:httpx-probe katana:katana-start nuclei:nuclei-probe; do
  name=${t%%:*}; marker=${t#*:}
  line=$(echo "$fl" | grep "/$marker" | head -1)
  got=$(echo "$line" | jget '(d.get("actor"), d.get("decision"), d.get("status"))')
  expect "$got" "('tool:$name', 'allowed', 200)" "$name goes through the guard, logged as tool:$name"
done

echo "== (j) every request in the flow log with actor / decision / reason"
fl=$(flows test-acme)
chk() {  # marker actor decision reason
  local n line
  n=$(echo "$fl" | grep -c "/$1\"" || true)
  line=$(echo "$fl" | grep "/$1\"" | head -1)
  expect "$n:$(echo "$line" | jget '(d["actor"], d["decision"], d["reason"], d["egress_ip"] is not None or d["reason"] is not None)')" \
         "1:('$2', '$3', $4, True)" "flow log: $1 -> $2 $3 $4"
}
chk b2 human allowed None
chk c1 human refused "'out-of-scope'"
chk d1 human refused "'not-in-scope'"
chk e1 human refused "'host-mismatch'"
chk f1 human refused "'internal-address'"
chk i1 claude allowed None
chk i2 "tool:mytool" allowed None
fl0=$(flows _none)
expect "$(echo "$fl0" | grep -c '"no-profile"')" 3 "no-profile refusals logged (flows/_none, program null)"
line=$(echo "$fl" | grep '/b2"' | head -1)
expect "$(echo "$line" | jget 'sorted(d)')" "['actor', 'category', 'decision', 'egress_ip', 'flow_id', 'host', 'ip', 'method', 'ms', 'program', 'reason', 'req_bytes', 'resp_bytes', 'status', 'ts', 'url']" "flow log line has the CONTRACTS §5 fields"
expect "$(echo "$line" | jget '(d["ip"], d["egress_ip"], d["category"])')" "('203.0.113.10', '198.51.100.20', 'target')" "flow log: ip, egress_ip, category"
mitm=$(as_root sh -c "cat $VAR/flows/test-acme/*.mitm | wc -c")
[ "$mitm" -gt 1000 ] && check ok ".mitm flow file written ($mitm bytes)" || check bad ".mitm flow file written"
leaks=$(docker exec "$T" python3 -c '
import json
bad = [l for l in open("/tmp/requests.log")
       if "x-intigriti-username" not in [k.lower() for k in json.loads(l)["headers"]]]
print(len(bad))')
expect "$leaks" 0 "target saw no request that bypassed the guard (all carry the guard's header)"

echo "== (h) rate limit (2 req/s, wait up to 10 s)"
sleep 2
start=$(python3 -c 'import time; print(time.time())')
res=$(docker exec "$C" bounty-run --actor human -- bash -c '
  for i in $(seq 1 25); do curl -s -o /dev/null -m 30 -w "%{http_code} %header{x-bounty-guard}\n" https://app.acme.test/h$i & done; wait')
secs=$(python3 -c "import time; print(round(time.time()-$start, 1))")
ok200=$(echo "$res" | grep -c '^200')
limited=$(echo "$res" | grep -c 'rate-limited')
echo "  25 parallel requests: $ok200 allowed, $limited rate-limited, ${secs}s"
python3 -c "import sys; sys.exit(0 if $secs >= 9.0 else 1)" && check ok "allowed requests spread over >= 9 s" || check bad "allowed requests spread over >= 9 s (${secs}s)"
[ "$ok200" -le 22 ] && [ "$limited" -ge 3 ] && [ $((ok200 + limited)) -eq 25 ] \
  && check ok "beyond 10 s of queue: refused rate-limited" || check bad "beyond 10 s of queue: refused rate-limited"
fl=$(flows test-acme)
expect "$(echo "$fl" | grep '"rate-limited"' | grep -c '/h[0-9]*"')" "$limited" "rate-limited refusals are in the flow log"

echo "== (k) guard killed: nothing leaves until the supervisor restarts it"
r0=$(as_root cat $VAR/guard.json | jget 'd["guard_restarts"]')
as_root pkill -9 -u guard
out=$(req human https://app.acme.test/k1)
expect "${out%% *}" "000" "proxy request fails while the guard is down"
out=$(docker exec "$C" bounty-run --actor human -- curl -s -m 3 --noproxy '*' -o /dev/null -w '%{http_code}' https://app.acme.test/k2 2>&1)
expect "$out" "000" "direct request fails while the guard is down"
back=""
for _ in $(seq 1 30); do
  [ "$(req human https://app.acme.test/k3 | cut -d' ' -f1)" = 200 ] && { back=yes; break; }
  sleep 0.5
done
expect "$back" yes "guard back within 15 s, traffic flows again (same profile, from tmpfs)"
r1=$(as_root cat $VAR/guard.json | jget 'd["guard_restarts"]')
[ "$r1" -gt "$r0" ] && check ok "guard.json counts the restart ($r0 -> $r1)" || check bad "guard.json counts the restart"

echo "== (k2) firewall tampered: guard stopped, rules restored"
as_root iptables -I OUTPUT 1 -j ACCEPT
for _ in $(seq 1 20); do as_root bounty-firewall check && break; sleep 0.5; done
as_root bounty-firewall check && check ok "supervisor re-locked the firewall" || check bad "supervisor re-locked the firewall"
as_root grep -q "firewall changed: guard stopped" $VAR/guard-events.log \
  && check ok "guard was stopped while the rules were wrong" || check bad "guard was stopped while the rules were wrong"
for _ in $(seq 1 30); do [ "$(req human https://app.acme.test/k4 | cut -d' ' -f1)" = 200 ] && break; sleep 0.5; done

echo "== (n) a sync changes the active program.json: reloaded without a manual step"
edit_profile() {  # python expression over p (the profile), written like a sync does (temp + rename)
  python3 - "$WS/programs/test-acme/program.json" "$1" <<'PY'
import json, os, sys
path, expr = sys.argv[1], sys.argv[2]
p = json.load(open(path))
exec(expr)
tmp = path + ".tmp"
json.dump(p, open(tmp, "w"), indent=1)
os.replace(tmp, path)
PY
}
expect "$(req human https://deep.app.acme.test/n1)" "200 " "deep.app.acme.test in scope before the change"
edit_profile 'p["scope"]["out"].append({"type": "domain", "value": "deep.app.acme.test", "description": ""}); p["scope"]["version"] = "2"'
start=$(python3 -c 'import time; print(time.time())')
got=""
for _ in $(seq 1 40); do
  got=$(req human https://deep.app.acme.test/n2)
  [ "$got" = "403 out-of-scope" ] && break
  sleep 0.5
done
secs=$(python3 -c "import time; print(round(time.time()-$start, 1))")
expect "$got" "403 out-of-scope" "host that just left scope refused without a manual command (${secs}s)"
python3 -c "import sys; sys.exit(0 if $secs <= 12 else 1)" && check ok "picked up within ~10 s" || check bad "picked up within ~10 s (${secs}s)"
expect "$(req human https://app.acme.test/n3)" "200 " "rest of the scope still allowed, identification kept"
wait_state 'd["scope_version"]' 2
# Only the newest record: older ones include failed reloads, whose "to" is {} (no scope_version).
expect_state '(lambda r: (r["trigger"], r["from"].get("scope_version"), r["to"].get("scope_version"), r["result"]))(d["reloads"][-1])' \
       "('program.json changed', '1', '2', 'active')" "reload logged in guard.json reloads (scope 1 -> 2)"
edit_profile 'p["rules"]["request_header"] = "X-Intigriti-Username: {Nickname}"; p["rules"]["version"] = "2"'
for _ in $(seq 1 40); do got=$(req human https://app.acme.test/n4); [ "$got" = "403 no-profile" ] && break; sleep 0.5; done
expect "$got" "403 no-profile" "profile now has an unknown placeholder: fail closed, refuse everything"
wait_state 'd["status"]' error
expect "$(state 'd["status"], "Nickname" in (d["last_error"] or "")')" "('error', True)" "guard.json says error and why"
edit_profile 'p["rules"]["request_header"] = "X-Intigriti-Username: {Username}"; p["rules"]["version"] = "3"'
for _ in $(seq 1 40); do got=$(req human https://app.acme.test/n5); [ "$got" = "200 " ] && break; sleep 0.5; done
expect "$got" "200 " "fixed profile picked up again, username kept from program use"
echo_json=$(body human https://app.acme.test/n6)
expect "$(echo "$echo_json" | jget '{k.lower(): v for k, v in d["headers"].items()}.get("x-intigriti-username")')" "tester" "header still filled after automatic reloads"

echo "== (o) nothing inside kali-bounty can change policy or evidence"
day=$(date -u +%Y%m%d)
try_as() {  # actor, shell snippet -> ok | denied
  docker exec "$C" bounty-run --actor "$1" -- bash -c "$2" >/dev/null 2>&1 && echo ok || echo denied
}
for actor in human claude; do
  expect "$(try_as $actor 'echo x >> /bounty/programs/test-acme/program.json')" denied "$actor: write program.json"
  expect "$(try_as $actor 'echo test-acme-int > /bounty/active')" denied "$actor: write /bounty/active"
  expect "$(try_as $actor 'echo {} > /bounty/config/recon-sources.json')" denied "$actor: write config/recon-sources.json"
  expect "$(try_as $actor 'mv /bounty/programs/test-acme /bounty/programs/x')" denied "$actor: rename a program folder"
  expect "$(try_as $actor "echo x >> $VAR/flows/test-acme/$day.jsonl")" denied "$actor: append to the flow log"
  expect "$(try_as $actor "echo x >> $VAR/guard.json")" denied "$actor: write guard.json"
  expect "$(try_as $actor "cat $VAR/ca/mitmproxy-ca.pem")" denied "$actor: read the CA key"
  expect "$(try_as $actor "echo x >> $VAR/ca/mitmproxy-ca.pem")" denied "$actor: write the CA key"
  expect "$(try_as $actor 'cat /dev/shm/bounty-guard-run/conf/mitmproxy-ca.pem')" denied "$actor: read the guard's tmpfs CA copy"
  expect "$(try_as $actor 'cat /dev/shm/bounty-guard/policy.json')" denied "$actor: read the policy snapshot (holds the username)"
  expect "$(try_as $actor 'echo {} > /dev/shm/bounty-guard/policy.json')" denied "$actor: write the policy snapshot"
  expect "$(try_as $actor 'echo x > recon/out-$RANDOM.txt && pwd | grep -qx /bounty/work/test-acme')" ok "$actor: writes its output in /bounty/work/test-acme/recon"
done
expect "$(as_root stat -c '%U:%G %a' /dev/shm/bounty-guard /dev/shm/bounty-guard/policy.json | tr '\n' ' ')" \
       "root:guard 750 root:guard 640 " "policy snapshot root-owned, not writable by hunter"
expect "$(as_root stat -c '%U:%G %a' $VAR $VAR/ca/mitmproxy-ca.pem $VAR/flows | tr '\n' ' ')" \
       "root:guard 750 guard:guard 400 guard:guard 750 " "volume: root:guard 0750, CA key guard-only, flows guard-owned"
expect "$(as_root sh -c 'grep -c "PRIVATE KEY" /usr/local/share/ca-certificates/bounty-guard-ca.crt /etc/ssl/certs/ca-certificates.crt' | tr '\n' ' ')" \
       "/usr/local/share/ca-certificates/bounty-guard-ca.crt:0 /etc/ssl/certs/ca-certificates.crt:0 " "trust store has the CA certificate only, no key"
expect "$(docker port "$C" | tr '\n' ' ')" "8080/tcp -> 127.0.0.1:$PORT " "only 8080 published (127.0.0.1); claude port 8082 is not"
expect "$(docker exec "$C" bounty-run --actor human -- curl -s -x http://127.0.0.1:8082 -o /dev/null -w '%{http_code}' -m 20 https://app.acme.test/o-claudeport 2>/dev/null)" \
       "200" "claude port 8082 works inside the container"
sleep 1
expect "$(flows test-acme | grep '/o-claudeport"' | jget 'd["actor"]')" claude "traffic on 8082 logged as claude"
out=$(host/bounty flows --handle test-acme --tail 500 --json | jget 'sum(1 for r in d if r["url"].endswith("/b2"))')
expect "$out" 1 "host/bounty flows --json reads the flow log via docker exec"
host/bounty flows --handle test-acme --tail 3 | grep -q "https://" && check ok "host/bounty flows (text)" || check bad "host/bounty flows (text)"
exp="$WS/export"
host/bounty flows export test-acme "$exp" >/dev/null 2>&1
ls "$exp"/test-acme-flows-*/"$day".jsonl "$exp"/test-acme-flows-*/"$day".mitm >/dev/null 2>&1 \
  && check ok "host/bounty flows export copies .jsonl + .mitm to a Mac folder" || check bad "host/bounty flows export copies .jsonl + .mitm to a Mac folder"

echo "== (p) recon map: only in-scope hosts, through the guard as tools (CONTRACTS §10.2)"
use test-acme
# Written from the Mac side, like `recon passive` does: the container must still be able to add to it.
mkdir -p "$WS/work/test-acme/recon"
cat > "$WS/work/test-acme/recon/hosts.jsonl" <<'EOF'
{"host": "app.acme.test", "scope": "in-scope", "resolves": true, "first_seen": "2026-11-01T00:00:00Z"}
{"host": "community.acme.test", "scope": "out-of-scope", "resolves": true, "first_seen": "2026-11-01T00:00:00Z"}
{"host": "unrelated.test", "scope": "in-scope", "resolves": true, "first_seen": "2026-11-01T00:00:00Z"}
EOF
before=$(flows test-acme | wc -l)
host/bounty recon map >"$WS/map.out" 2>"$WS/map.err"; rc=$?
expect "$rc" 0 "recon map exits 0"
grep -q "bounty: progress: 3/3 done" "$WS/map.err" && check ok "recon map streams progress" || check bad "recon map streams progress ($(tail -2 "$WS/map.err"))"
got=$(python3 -c 'import json,sys; print(sorted({json.loads(l)["host"] for l in open(sys.argv[1]) if l.strip()}))' "$WS/work/test-acme/recon/live.jsonl" 2>/dev/null)
expect "$got" "['acme.test', 'app.acme.test', 'other.test']" "mapped: scope entries + in-scope resolving hosts; never out-of-scope or guard-refused names"
got=$(python3 -c 'import json,sys; print(sorted({r["status"] for r in map(json.loads, open(sys.argv[1])) if r["host"]=="app.acme.test"}))' "$WS/work/test-acme/recon/live.jsonl" 2>/dev/null)
expect "$got" "[200]" "httpx result recorded for app.acme.test"
[ -s "$WS/work/test-acme/recon/urls.jsonl" ] && check ok "katana URLs recorded" || check bad "katana URLs recorded"
sleep 1
new=$(flows test-acme | tail -n +"$((before + 1))")
for a in tool:httpx-toolkit tool:katana; do
  n=$(echo "$new" | python3 -c 'import json,sys; print(sum(1 for l in sys.stdin if l.strip() and json.loads(l).get("actor")==sys.argv[1] and json.loads(l).get("decision")=="allowed"))' "$a")
  [ "$n" -gt 0 ] && check ok "map traffic in the flow log as $a" || check bad "map traffic in the flow log as $a"
done
n=$(echo "$new" | python3 -c 'import json,sys; print(sum(1 for l in sys.stdin if l.strip() and json.loads(l).get("host") in ("community.acme.test","unrelated.test")))')
expect "$n" 0 "map sent nothing towards out-of-scope / not-in-scope names"
got=$(host/bounty recon list test-acme --json | jget '[(r["host"], r["live"]) for r in d if r["host"]=="app.acme.test"]')
expect "$got" "[('app.acme.test', True)]" "recon list shows the mapped host as live"

echo "== (q) two-account access-control check (CONTRACTS §13)"
use test-acme
SA=aaaaaaaaaaaaaaaaaaaa; SB=bbbbbbbbbbbbbbbbbbbb
for pth in idor protected public own app.js; do
  docker exec "$C" bounty-run --actor human -- curl -s -o /dev/null -b "sid=$SA; theme=dark" "https://app.acme.test/ac/$pth"
done
docker exec "$C" bounty-run --actor human -- curl -s -o /dev/null -b "sid=$SA" -X POST -d x=1 https://app.acme.test/ac/write
printf 'Cookie: sid=%s\n' "$SA" | host/bounty ac session a --marker alice@acme.test >/dev/null 2>&1; rc=$?
expect "$rc" 0 "ac session a from stdin"
printf 'Cookie: sid=%s\n' "$SB" | host/bounty ac session b >/dev/null 2>&1
printf 'nonsense\n' | host/bounty ac session b >/dev/null 2>&1; rc=$?
expect "$rc" 64 "ac session refuses input without a header line"
printf 'Cookie: sid=%s\n' "$SB" | host/bounty ac session b >/dev/null 2>&1
got=$(host/bounty ac status --json | jget '(d["a"]["names"], d["a"]["markers"], d["b"]["names"])')
expect "$got" "(['Cookie'], 1, ['Cookie'])" "ac status shows header names only"
host/bounty ac status --json | grep -q "$SA" && check bad "ac status never prints a session value" || check ok "ac status never prints a session value"
cand=$(host/bounty ac candidates --json)
got=$(echo "$cand" | jget 'sorted((r["url"].rsplit("/",1)[1], r["safe"]) for r in d)')
expect "$got" "[('idor', True), ('own', True), ('protected', True), ('public', True), ('write', False)]" "candidates: A's requests, static files skipped, POST marked unsafe"
ids=$(echo "$cand" | jget '" ".join(r["flow_id"] for r in d if r["safe"])')
wid=$(echo "$cand" | jget '[r["flow_id"] for r in d if not r["safe"]][0]')
host/bounty ac replay "$wid" >/dev/null 2>&1; rc=$?
expect "$rc" 64 "ac replay refuses a POST without --unsafe"
before=$(docker exec "$T" sh -c 'wc -l < /tmp/requests.log')
host/bounty ac replay $ids >"$WS/ac.out" 2>"$WS/ac.err"; rc=$?
expect "$rc" 0 "ac replay exits 0"
got=$(host/bounty ac results --json | jget 'sorted((r["url"].rsplit("/",1)[1], r["hint"], r["b"]["verdict"], r["none"]["verdict"]) for r in d)')
expect "$got" "[('idor', 'likely-broken-access', 'leak', 'blocked'), ('own', 'none', 'differs', 'blocked'), ('protected', 'none', 'blocked', 'blocked'), ('public', 'public', 'same-as-a', 'same-as-a')]" "verdicts: IDOR flagged, own data and 403 not, public marked public"
sent=$(docker exec "$T" sh -c "tail -n +$((before + 1)) /tmp/requests.log")
n=$(echo "$sent" | grep -c "sid=$SA")
expect "$n" 0 "replays never carried account A's session"
n=$(echo "$sent" | grep -c "sid=$SB")
expect "$n" 4 "each request replayed once as B"
n=$(echo "$sent" | grep -c '"/ac/write"')
expect "$n" 0 "the POST was not replayed"
sleep 1
n=$(flows test-acme | python3 -c 'import json,sys; print(sum(1 for l in sys.stdin if l.strip() and json.loads(l).get("actor")=="tool:ac-check"))')
expect "$n" 8 "replays in the flow log as tool:ac-check (B + none per request)"
grep -rq "$SA" "$WS/work" && check bad "no session value written to the workspace" || check ok "no session value written to the workspace"
as_root sh -c 'stat -c %a /dev/shm/bounty-ac/test-acme/a.json' | grep -q '^600$' && check ok "sessions in tmpfs, root 0600" || check bad "sessions in tmpfs, root 0600"

echo "== (r) curated exposure checks on mapped live hosts (CONTRACTS §10.6)"
use test-acme
before=$(flows test-acme | wc -l)
host/bounty recon exposures >"$WS/exp.out" 2>"$WS/exp.err"; rc=$?
expect "$rc" 0 "recon exposures exits 0"
grep -q "bounty: progress: 2/2 done" "$WS/exp.err" && check ok "recon exposures streams progress" || check bad "recon exposures streams progress ($(tail -2 "$WS/exp.err"))"
got=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sorted({h["check"] for h in d["hits"]}), sorted({h["host"] for h in d["hits"]}), sorted(d["checked"]))' "$WS/work/test-acme/recon/exposures.json" 2>/dev/null)
expect "$got" "['kl-env-file', 'kl-git-config'] ['acme.test', 'app.acme.test'] ['acme.test', 'app.acme.test']" "only the planted files are reported, on the live hosts only (other.test is in scope only under /allowed, so never live); catch-all 200 pages don't match"
grep -rq FAKE_GUARD_TEST_VALUE "$WS/work" && check bad "exposed file contents never stored" || check ok "exposed file contents never stored"
sleep 1
new=$(flows test-acme | tail -n +"$((before + 1))")
n=$(echo "$new" | python3 -c 'import json,sys; rs=[json.loads(l) for l in sys.stdin if l.strip()]; print(len(rs) > 0 and all(r.get("actor")=="tool:nuclei" for r in rs))')
expect "$n" "True" "every exposure request in the flow log as tool:nuclei"
got=$(host/bounty recon list test-acme --json | jget '[(r["exposures"], "exposed" in r["signals"]) for r in d if r["host"]=="app.acme.test"]')
expect "$got" "[(['kl-env-file', 'kl-git-config'], True)]" "recon list shows the exposures"

echo "== (l) rate_limit_rps 0: only human traffic"
use test-acme-norate
expect "$(req human https://app.acme.test/l1)" "200 " "human (terminal, Burp) allowed"
expect "$(req claude https://app.acme.test/l2)" "403 automation-forbidden" "claude refused automation-forbidden"
docker exec "$C" bounty-run --actor human -- httpx-toolkit -silent -duc -u https://app.acme.test/l3 >/dev/null 2>&1
sleep 1
line=$(flows test-acme-norate | grep '/l3' | head -1)
expect "$(echo "$line" | jget '(d["actor"], d["decision"], d["reason"])')" "('tool:httpx-toolkit', 'refused', 'automation-forbidden')" "hunter tool refused automation-forbidden"
docker exec "$C" bounty-run --actor claude -- ffuf -u https://app.acme.test/FUZZ -w /home/hunter/words >/dev/null 2>&1; rc=$?
expect "$rc" 6 "bounty-run --actor claude with an active tool exits 6"
host/bounty recon map >/dev/null 2>&1; rc=$?
expect "$rc" 6 "recon map refused when the program forbids automation"
host/bounty ac replay "$wid" >/dev/null 2>&1; rc=$?
expect "$rc" 6 "ac replay refused when the program forbids automation"
host/bounty recon exposures >/dev/null 2>&1; rc=$?
expect "$rc" 6 "recon exposures refused when the program forbids automation"

echo "== (m) a profile that breaks while active: refuse all and say why"
echo test-broken > "$WS/active"
as_root bounty-ctl reload --expect test-broken >/dev/null 2>&1; rc=$?
expect "$rc" 4 "bounty-ctl reload of a broken profile exits 4"
out=$(body human https://app.acme.test/m1)
case "$out" in *"no active program"*"not valid JSON"*) check ok "refused no-profile, body says why";;
  *) check bad "refused no-profile, body says why (got: $out)";; esac
wait_state 'd["status"]' error
expect "$(state 'd["status"]')" "error" "guard.json status error"

[ -n "${BOUNTY_TEST_KEEP:-}" ] && { echo "kept $C, $T, $WS"; exit $fail; }
echo "== guard stop"
host/bounty guard stop >/dev/null && ! docker inspect "$C" >/dev/null 2>&1 && [ -d "$WS/programs" ] \
  && docker volume inspect "$VOL" >/dev/null 2>&1 \
  && check ok "guard stop removes the container, keeps the workspace and the flows volume" \
  || check bad "guard stop removes the container, keeps the workspace and the flows volume"
host/bounty guard stop --purge >/dev/null 2>&1; rc=$?
expect "$rc:$(docker volume inspect "$VOL" >/dev/null 2>&1 && echo kept)" "64:kept" "guard stop --purge without --yes refuses"
host/bounty guard stop --purge --yes >/dev/null 2>&1
docker volume inspect "$VOL" >/dev/null 2>&1 && check bad "guard stop --purge --yes removes the volume" || check ok "guard stop --purge --yes removes the volume"

[ $fail -eq 0 ] && echo "ALL PASS" || { echo "SOME CHECKS FAILED"; exit 1; }
