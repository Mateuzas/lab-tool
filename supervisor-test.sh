#!/usr/bin/env bash
# Breaks the lab on purpose and checks the supervisor recovers (or explains) each failure.
# Run on the Mac with the lab connected:  ./supervisor-test.sh [vpn-filter]   (default: starting)
# Needs no target machine: health is the HTB VPN gateway.
set -uo pipefail
cd "$(dirname "$0")"

FILTER=${1:-starting}
in_kali() { docker exec kali-lab bash -c "$1"; }
field() { python3 -c 'import json,sys;print(json.load(open("state/status.json")).get(sys.argv[1],""))' "$1" 2>/dev/null; }
fail=0
# wait_for <state> <max seconds> <description> [after-epoch]
# With after-epoch, only a state the supervisor entered at or after that time counts.
wait_for() {
  local t0=$(date +%s) after=${4:-0}
  while [ $(( $(date +%s) - t0 )) -lt "$2" ]; do
    if [ "$(field state)" = "$1" ] && [ "$(field state_since)" -ge "$after" ]; then
      echo "  PASS: $3 -> $1 in $(( $(date +%s) - t0 ))s ($(field message))"
      return 0
    fi
    sleep 1
  done
  echo "  FAIL: $3 -> still '$(field state)' after $2s ($(field message))"
  fail=1
}

echo "== 0. baseline"
wait_for connected 30 "lab connected"

echo "== 1. container restart (no script run afterwards)"
t=$(date +%s); docker restart kali-lab >/dev/null
wait_for connected 40 "comes back by itself" "$t"

echo "== 2. VPN process killed"
before=$(field vpn_restarts); t=$(date +%s)
in_kali 'pkill -9 -x openvpn'
ok=""
for _ in $(seq 1 30); do
  if [ "$(field vpn_restarts)" -gt "$before" ] && [ "$(field updated)" -gt "$t" ] \
     && [ "$(field state)" = connected ] && [ -n "$(field gateway_ms)" ] \
     && in_kali 'ip -br addr show tun0' >/dev/null 2>&1; then ok=yes; break; fi
  sleep 1
done
[ -n "$ok" ] && echo "  PASS: VPN restarted in $(( $(date +%s) - t ))s (restarts $before -> $(field vpn_restarts), gateway $(field gateway_ms) ms)" \
  || { echo "  FAIL: VPN not back (restarts $(field vpn_restarts), state $(field state))"; fail=1; }

echo "== 3. proxy killed"
in_kali 'pkill -9 -x microsocks'
sleep 5
[ "$(field proxy)" = up ] && echo "  PASS: proxy restarted" || { echo "  FAIL: proxy still down"; fail=1; }

echo "== 4. kill switch removed"
in_kali 'iptables -P OUTPUT ACCEPT; iptables -F OUTPUT'
sleep 5
[ "$(field kill_switch)" = on ] && echo "  PASS: kill switch re-applied" || { echo "  FAIL: kill switch is $(field kill_switch)"; fail=1; }

echo "== 5. network to HTB cut (like Wi-Fi dropping)"
server_ips=$(in_kali "getent ahostsv4 \$(awk '/^remote /{print \$2; exit}' /lab/\$(cat /lab/state/selected-vpn)) | awk '{print \$1}' | sort -u")
for ip in $server_ips; do in_kali "iptables -I OUTPUT 1 -d $ip -j DROP"; done
t=$(date +%s)
wait_for reconnecting 90 "outage noticed" "$t"
t=$(date +%s); for ip in $server_ips; do in_kali "iptables -D OUTPUT -d $ip -j DROP"; done
wait_for connected 60 "recovers when the network returns" "$t"

echo "== 6. disconnect and reconnect"
./vpn-up.sh off >/dev/null
wait_for disconnected 10 "disconnected"
[ "$(field kill_switch)" = block ] && echo "  PASS: everything blocked while disconnected" || { echo "  FAIL: kill switch is $(field kill_switch)"; fail=1; }
./vpn-up.sh "$FILTER" >/dev/null
wait_for connected 30 "reconnected"

echo "== 7. VPN file with script hooks is refused"
printf 'client\ndev tun\nremote 192.0.2.1 1194\nscript-security 2\nup /bin/evil.sh\n' > zz-hooked-test.ovpn
echo zz-hooked-test.ovpn > state/selected-vpn
wait_for error 10 "hooked file refused"
rm -f zz-hooked-test.ovpn
./vpn-up.sh "$FILTER" >/dev/null
wait_for connected 30 "back on the real file"

[ $fail -eq 0 ] && echo "ALL PASS" || { echo "SOME CHECKS FAILED"; exit 1; }
