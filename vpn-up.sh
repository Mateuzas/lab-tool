#!/usr/bin/env bash
# Tasks 2.2-2.8 and 3.3: start the lab VPN and the eth0-only SOCKS proxy in the kali-lab container.
# Run on the Mac: ./vpn-up.sh [name-filter]   e.g. ./vpn-up.sh starting  or  ./vpn-up.sh machines
# Keep one .ovpn per HTB section in this folder; the filter picks which one to connect with.
set -euo pipefail

FILTER=${1:-}

in_kali() { docker exec kali-lab bash -c "$1"; }

docker start kali-lab >/dev/null

echo "== 2.2 pick exactly one VPN file"
matches=$(in_kali "ls /lab/*.ovpn 2>/dev/null | grep -i -- '$FILTER' || true")
count=$(printf '%s' "$matches" | grep -c . || true)
if [ "$count" -ne 1 ]; then
  echo "FAIL: '${FILTER:-*}' matches $count .ovpn files in ~/Desktop/Projects/kali-lab:"
  in_kali 'for f in /lab/*.ovpn; do echo "  $(basename "$f")  ->  $(grep -m1 "^remote " "$f" | cut -d" " -f2)"; done' || true
  echo "Pass a word from one file name, e.g. ./vpn-up.sh starting"
  exit 1
fi
OVPN=$matches
echo "using $(basename "$OVPN")"
in_kali "echo '$OVPN' > /tmp/ovpn.path"

echo "== 2.3 hook check"
if in_kali "grep -nE '^(script-security|up|down|route-up|route-pre-down|plugin|tls-verify|auth-user-pass-verify)' '$OVPN'"; then
  echo "FAIL: the .ovpn contains script hooks. Do not connect. Re-download it from your HTB account."
  exit 1
fi
echo "ok, no hooks"

echo "== kill switch"
in_kali "sh /lab/kill-switch.sh on '$OVPN'"

echo "== 2.4/2.5 start tunnel"
if ! in_kali "pkill openvpn || true; sleep 1; openvpn --config '$OVPN' --daemon --log /tmp/ovpn.log"; then
  echo "FAIL: openvpn refused to start. Log:"
  in_kali 'tail -n 8 /tmp/ovpn.log'
  exit 1
fi
for _ in $(seq 1 30); do
  in_kali 'grep -q "Initialization Sequence Completed" /tmp/ovpn.log' && break
  sleep 1
done
if ! in_kali 'grep -q "Initialization Sequence Completed" /tmp/ovpn.log'; then
  echo "FAIL: tunnel did not come up. Last log lines:"
  in_kali 'tail -n 8 /tmp/ovpn.log'
  exit 1
fi
echo "ok, Initialization Sequence Completed"

echo "== 2.6 tun0"
in_kali 'ip -br addr show tun0'

echo "== 2.7 clock (mac vs kali, epoch seconds)"
echo "mac $(date -u +%s)  kali $(in_kali 'date -u +%s')"

echo "== 2.8 routes"
in_kali 'ip route; echo; ip route get 8.8.8.8'

echo "== 3.3 proxy on eth0 only"
in_kali 'pkill microsocks || true
ETH0_IP=$(ip -4 -o addr show eth0 | awk "{print \$4}" | cut -d/ -f1)
(setsid microsocks -i "$ETH0_IP" -p 1080 >/tmp/socks.log 2>&1 &)
sleep 1; ss -ltn | grep 1080'

echo "== 3.4 Mac side"
lsof -nP -iTCP:1080 -sTCP:LISTEN

echo
echo "Next: spawn a machine, then run ./target-test.sh <TARGET_IP> [PORT]"
