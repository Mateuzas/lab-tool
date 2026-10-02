#!/usr/bin/env bash
# Proves the kill switch (Review Focus 2 + 5). Run on the Mac with the VPN up:
#   tests/kill-switch-test.sh <TARGET_IP> [PORT]
# Only use an IP of a machine you spawned yourself.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1   # repo root
WS=${LAB_WORKSPACE:-workspace}

TARGET=${1:?usage: tests/kill-switch-test.sh <TARGET_IP> [PORT]}
PORT=${2:-80}
in_kali() { docker exec kali-lab bash -c "$1"; }
via_proxy() { curl -sS -m 10 --socks5-hostname 127.0.0.1:1080 -o /dev/null -w '%{http_code}' "$1" 2>&1; }
fail=0
check() { if [ "$1" = ok ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }

echo "== 1. lab target reachable with VPN up"
code=$(via_proxy "http://$TARGET:$PORT/"); echo "  got: $code"
[ "$code" = 200 ] && check ok "target answers" || check bad "target answers"

echo "== 2. non-lab destination blocked (internet via proxy)"
out=$(via_proxy "http://example.com/"); echo "  got: $out"
[[ "$out" == *200* ]] && check bad "internet blocked" || check ok "internet blocked"

echo "== 3. VPN drop: nothing for 10.0.0.0/8 may leave via eth0"
in_kali 'rm -f /tmp/leak.pcap; (setsid tcpdump -n -i eth0 -w /tmp/leak.pcap "net 10.0.0.0/8" >/dev/null 2>&1 &)'
sleep 1
touch "$WS/state/hold"; in_kali 'pkill -x openvpn'; sleep 2
start=$(python3 -c 'import time; print(time.time())')
out=$(via_proxy "http://$TARGET:$PORT/")
secs=$(python3 -c "import time; print(round(time.time()-$start, 2))")
echo "  got: $out (after ${secs}s)"
[[ "$out" == *200* ]] && check bad "request fails with VPN down" || check ok "request fails with VPN down"
python3 -c "import sys; sys.exit(0 if $secs < 3 else 1)" && check ok "fails fast (<3 s)" || check bad "fails fast (<3 s)"
in_kali 'ping -c 2 -W 1 '"$TARGET"' >/dev/null 2>&1; nmap -Pn -p '"$PORT"' --max-retries 0 '"$TARGET"' >/dev/null 2>&1; sleep 1; pkill tcpdump; sleep 1'
leaked=$(in_kali 'tcpdump -n -r /tmp/leak.pcap 2>/dev/null | wc -l')
echo "  packets to 10.0.0.0/8 seen on eth0: $leaked"
[ "$leaked" -eq 0 ] && check ok "no lab traffic leaked to the host network" || check bad "no lab traffic leaked to the host network"

echo "== 4. VPN reconnects through the kill switch"
rm -f "$WS/state/hold"
for _ in $(seq 1 40); do
  grep -q '"state":"connected"' "$WS/state/status.json" 2>/dev/null && break
  sleep 1
done
code=$(via_proxy "http://$TARGET:$PORT/"); echo "  got: $code"
[ "$code" = 200 ] && check ok "target answers again" || check bad "target answers again"

[ $fail -eq 0 ] && echo "ALL PASS" || { echo "SOME CHECKS FAILED"; exit 1; }
