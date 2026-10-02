#!/usr/bin/env bash
# Connect the lab. The supervisor in the container does the work; this picks the VPN file,
# makes sure the container runs, and waits for the status.
#   ./vpn-up.sh [name-filter]   e.g. ./vpn-up.sh starting  or  ./vpn-up.sh machines
#   ./vpn-up.sh off             disconnect (kill switch stays closed)
# Keep one .ovpn per HTB section in this folder; the filter picks which one.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p state

ensure_container() {
  if ! docker inspect kali-lab >/dev/null 2>&1; then
    docker run -d --name kali-lab --init --restart unless-stopped \
      --cap-add=NET_ADMIN --device /dev/net/tun \
      -p 127.0.0.1:1080:1080 -v "$PWD":/lab kali-lab:spike >/dev/null
  fi
  docker start kali-lab >/dev/null
}

if [ "${1:-}" = off ]; then
  : > state/selected-vpn
  echo "disconnected"
  exit 0
fi

FILTER=${1:-}
matches=$(ls ./*.ovpn 2>/dev/null | grep -i -- "$FILTER" || true)
count=$(printf '%s' "$matches" | grep -c . || true)
if [ "$count" -ne 1 ]; then
  echo "'${FILTER:-*}' matches $count .ovpn files here:"
  for f in ./*.ovpn; do [ -e "$f" ] && echo "  $(basename "$f")  ->  $(grep -m1 '^remote ' "$f" | cut -d' ' -f2)"; done
  echo "Pass a word from one file name, e.g. ./vpn-up.sh starting"
  exit 1
fi

sel=$(basename "$matches")
t0=$(date +%s)
echo "$sel" > state/selected-vpn
ensure_container
echo "using $(cat state/selected-vpn)"

for _ in $(seq 1 30); do
  st=$(python3 -c 'import json,sys;d=json.load(open("state/status.json"))
print(d["state"] if d["vpn_file"]==sys.argv[1] and d["updated"]>int(sys.argv[2]) else "starting")' "$sel" "$t0" 2>/dev/null || echo starting)
  case "$st" in connected|error) break ;; esac
  sleep 2
done
python3 -m json.tool state/status.json
[ "$st" = connected ]
