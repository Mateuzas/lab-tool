#!/usr/bin/env bash
# Wireshark gate: capture lab traffic on the container's tun0.
#   ./capture.sh live              stream live into native Wireshark on the Mac
#   ./capture.sh save [seconds]    save to captures/<time>.pcap (default 30 s)
#   ./capture.sh summary <file>    text summary of a saved capture (what the tutor reads)
set -euo pipefail
cd "$(dirname "$0")"

WIRESHARK=/Applications/Wireshark.app/Contents/MacOS/Wireshark
mode=${1:-live}

case "$mode" in
  live)
    [ -x "$WIRESHARK" ] || { echo "Wireshark is not installed in /Applications."; exit 1; }
    # -U: flush each packet so Wireshark updates live. -i (no -t) keeps the pipe binary-clean.
    docker exec -i kali-lab tcpdump -U -n -i tun0 -w - 2>/dev/null | "$WIRESHARK" -k -i - ;;
  save)
    secs=${2:-30}
    mkdir -p captures
    out="captures/$(date +%Y%m%d-%H%M%S).pcap"
    echo "capturing tun0 for ${secs}s -> $out"
    docker exec kali-lab timeout "$secs" tcpdump -U -n -i tun0 -w "/lab/$out" 2>&1 | tail -2 || true
    echo "$out" ;;
  summary)
    f=${2:?usage: ./capture.sh summary captures/<file>.pcap}
    docker exec kali-lab sh -c "
      echo '== protocols'; tshark -r '/lab/$f' -q -z io,phs 2>/dev/null | sed -n '/Protocol Hierarchy/,/====/p' | head -30
      echo '== conversations'; tshark -r '/lab/$f' -q -z conv,tcp 2>/dev/null | head -20
      echo '== first packets'; tshark -r '/lab/$f' -c 15 2>/dev/null" ;;
  *)
    echo "usage: ./capture.sh live | save [seconds] | summary <file>"; exit 1 ;;
esac
