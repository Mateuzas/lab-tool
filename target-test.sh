#!/usr/bin/env bash
# Tasks 2.9, 3.5 and 3.6: reach a spawned lab target, through the proxy, then prove a dropped
# tunnel fails visibly. Run on the Mac: ./target-test.sh <TARGET_IP> [PORT]
# Only use an IP of a machine you spawned yourself.
set -uo pipefail

TARGET=${1:?usage: ./target-test.sh <TARGET_IP> [PORT]}
PORT=${2:-80}
in_kali() { docker exec kali-lab bash -c "$1"; }
via_proxy() { curl -sS -m 10 --socks5-hostname 127.0.0.1:1080 -o /dev/null -w '%{http_code}' "http://$TARGET:$PORT/"; }

echo "== 2.9 ping + fast nmap from the container"
in_kali "ping -c 3 -W 2 $TARGET; nmap -Pn -F $TARGET"

echo "== 3.5 HTTP through the proxy (port $PORT)"
code=$(via_proxy) || true
echo "status: $code"
[[ "$code" =~ ^[1-5][0-9][0-9]$ && "$code" != "000" ]] || { echo "FAIL: no HTTP response. Check PORT matches an open web port from nmap."; exit 1; }

echo "== 3.6 tunnel-drop test"
touch state/hold; in_kali 'pkill -x openvpn'; sleep 3
if code=$(via_proxy); then
  echo "FAIL (Review Focus 2): got HTTP $code with the tunnel down. Traffic leaked via the host network."
  dropped=leak
else
  echo "ok: request failed with the tunnel down"
  dropped=ok
fi

echo "== restore tunnel"
rm -f state/hold
for _ in $(seq 1 30); do in_kali 'ip -br addr show tun0' >/dev/null 2>&1 && break; sleep 1; done
sleep 3
code=$(via_proxy) || true
echo "after restore: $code"

[ "$dropped" = ok ] && echo "PASS: Tasks 2.9, 3.5, 3.6" || exit 1
