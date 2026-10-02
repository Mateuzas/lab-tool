#!/usr/bin/env bash
# Scope enforcement (Review Focus 5). Needs the lab connected, but no running machine:
# it proves where packets go by watching tun0, not by talking to a target.
#   tests/scope-test.sh [in-scope-ip] [out-of-scope-ip]
# Defaults are two lab addresses; out-of-scope packets must never leave the container.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
WS=${LAB_WORKSPACE:-workspace}

IN=${1:-10.129.14.194}
OUT=${2:-10.129.14.195}
in_kali() { docker exec kali-lab bash -c "$1"; }
field() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$WS/state/status.json" "$1" 2>/dev/null; }
fail=0
check() { if [ "$1" = ok ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }
# set_scope <ips...>: write the scope and wait until the supervisor has applied it
set_scope() {
  local want="$*"
  [ "$want" = clear ] && want=""
  host/lab target "$@" >/dev/null
  for _ in $(seq 1 20); do
    [ "$(field targets)$(field blocked_targets)" = "$want" ] && return
    sleep 1
  done
}
# packets_on_tun0 <ip>: try to reach ip:80 through the proxy, count packets for it on tun0
packets_on_tun0() {
  in_kali "rm -f /tmp/scope.pcap; (setsid tcpdump -n -i tun0 -w /tmp/scope.pcap host $1 >/dev/null 2>&1 &)"
  sleep 1
  curl -s -m 3 --socks5-hostname 127.0.0.1:1080 -o /dev/null "http://$1/" 2>/dev/null
  sleep 1
  in_kali "pkill -x tcpdump; sleep 1; tcpdump -n -r /tmp/scope.pcap 2>/dev/null | wc -l"
}

echo "== 1. internet address refused as a target"
set_scope 8.8.8.8
[ "$(field blocked_targets)" = 8.8.8.8 ] && [ -z "$(field targets)" ] \
  && check ok "8.8.8.8 refused ($(field message))" || check bad "8.8.8.8 refused (targets='$(field targets)')"

echo "== 2. malformed address refused"
set_scope 10.129.14
[ "$(field blocked_targets)" = 10.129.14 ] && check ok "typo refused" || check bad "typo refused"

echo "== 3. in-scope lab address: packets go out through the VPN"
set_scope "$IN"
[ "$(field targets)" = "$IN" ] && check ok "target accepted: $IN" || check bad "target accepted (got '$(field targets)')"
n=$(packets_on_tun0 "$IN"); echo "  packets for $IN on tun0: $n"
[ "$n" -gt 0 ] && check ok "in-scope traffic leaves via VPN" || check bad "in-scope traffic leaves via VPN"

echo "== 4. out-of-scope lab address: nothing leaves"
n=$(packets_on_tun0 "$OUT"); echo "  packets for $OUT on tun0: $n"
[ "$n" -eq 0 ] && check ok "out-of-scope traffic blocked in the container" || check bad "out-of-scope traffic blocked"

echo "== 5. VPN health still works with an empty scope"
set_scope clear
[ "$(field state)" = connected ] && [ -n "$(field gateway_ms)" ] \
  && check ok "gateway reachable ($(field gateway_ms) ms)" || check bad "gateway reachable"

[ $fail -eq 0 ] && echo "ALL PASS" || { echo "SOME CHECKS FAILED"; exit 1; }
