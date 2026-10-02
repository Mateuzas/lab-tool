#!/usr/bin/env bash
# Lab supervisor: the container's main process. Keeps the kill switch, the VPN and the proxy up,
# and writes /lab/state/status.json for the app (state + plain-language message).
#
# Inputs (written by the Mac side):
#   /lab/state/selected-vpn   basename of the .ovpn to use (empty/missing = stay disconnected)
#   /lab/state/hold           if present, do not restart the VPN (used by drop tests)
set -u

STATE=/lab/state
STATUS=$STATE/status.json
EVENTS=$STATE/events.log
VPN_LOG=/tmp/ovpn.log
HOOKS='^(script-security|up|down|route-up|route-pre-down|ipchange|plugin|tls-verify|auth-user-pass-verify|client-connect|learn-address)'
mkdir -p "$STATE"

vpn_pid=""; current=""; vpn_started=0; restarts=0; gw_fails=0; ever_connected=""
state=starting; message="Starting the lab"; state_since=$(date +%s); gateway_ms=""; refused=""

log() { echo "$(date -u +%FT%TZ) $*" >> "$EVENTS"; }

set_state() {
  if [ "$1" != "$state" ]; then
    log "state $state -> $1: $2"
    state_since=$(date +%s)
  fi
  state=$1; message=$2
}

write_status() {
  local lab_ip proxy ks server=""
  lab_ip=$(ip -4 -o addr show tun0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  ss -ltn 2>/dev/null | grep -q ':1080 ' && proxy=up || proxy=down
  if iptables -S OUTPUT 2>/dev/null | grep -q -- '-o tun0'; then ks=on
  elif iptables -S OUTPUT 2>/dev/null | grep -q REJECT; then ks=block
  else ks=off; fi
  [ -n "$current" ] && server=$(awk '/^remote /{print $2; exit}' "/lab/$current" 2>/dev/null)
  printf '{"state":"%s","message":"%s","vpn_file":"%s","server":"%s","lab_ip":"%s","gateway_ms":"%s","proxy":"%s","kill_switch":"%s","vpn_restarts":%d,"state_since":%d,"updated":%d}\n' \
    "$state" "$message" "$current" "$server" "$lab_ip" "$gateway_ms" "$proxy" "$ks" \
    "$restarts" "$state_since" "$(date +%s)" > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
}

stop_vpn() {
  pkill -x openvpn 2>/dev/null
  vpn_pid=""
}

vpn_alive() { [ -n "$vpn_pid" ] && kill -0 "$vpn_pid" 2>/dev/null; }

start_vpn() {
  # HTB pushes ping-restart 120; override so a dead tunnel is noticed in ~20 s.
  openvpn --config "/lab/$current" --log "$VPN_LOG" \
    --pull-filter ignore ping --ping 5 --ping-restart 20 >/dev/null 2>&1 &
  vpn_pid=$!
  vpn_started=$(date +%s)
  log "openvpn started (pid $vpn_pid, $current)"
}

ensure_kill_switch() {
  if ! iptables -S OUTPUT 2>/dev/null | grep -q -- '-o tun0'; then
    if ! sh /lab/kill-switch.sh on "/lab/$current" >/dev/null 2>&1; then
      sh /lab/kill-switch.sh block >/dev/null 2>&1
      return 1
    fi
    log "kill switch on"
  fi
}

ensure_proxy() {
  local eth0
  eth0=$(ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1)
  if ! ss -ltn | grep -q "$eth0:1080 "; then
    pkill -x microsocks 2>/dev/null
    setsid microsocks -i "$eth0" -p 1080 >/tmp/socks.log 2>&1 &
    log "proxy started on $eth0:1080"
  fi
}

# Map OpenVPN log lines to something a beginner can act on.
classify_error() {
  local tail
  tail=$(tail -n 40 "$VPN_LOG" 2>/dev/null)
  case "$tail" in
    *AUTH_FAILED*)               echo "HTB rejected this VPN file. Download a fresh one from HTB." ;;
    *"Options error"*)           echo "This VPN file is damaged. Download it again from HTB." ;;
    *"Cannot open TUN/TAP"*)     echo "The lab cannot create its VPN device. Restart Docker Desktop." ;;
    *"Cannot resolve host"*|*RESOLVE*) echo "Cannot find HTB's VPN server. Check your internet connection." ;;
    *"TLS key negotiation failed"*|*"TLS Error"*) echo "Cannot reach HTB's VPN server. Your network may block it; try HTB's TCP VPN file." ;;
    *) echo "" ;;
  esac
}

trap 'stop_vpn; log "supervisor stopped"; exit 0' TERM INT
log "supervisor started"
sh /lab/kill-switch.sh block >/dev/null 2>&1   # closed until a VPN is chosen

while true; do
  selected=$(cat "$STATE/selected-vpn" 2>/dev/null | tr -d '[:space:]')

  if [ "$selected" != "$current" ]; then
    stop_vpn; current=$selected; refused=""; restarts=0; ever_connected=""
    sh /lab/kill-switch.sh block >/dev/null 2>&1
    if [ -n "$current" ] && [ -f "/lab/$current" ] && grep -qE "$HOOKS" "/lab/$current"; then
      refused=yes
      log "refused $current: contains script hooks"
    fi
  fi

  ensure_proxy

  if [ -z "$current" ]; then
    set_state disconnected "Choose a lab to connect to."
  elif [ ! -f "/lab/$current" ]; then
    set_state error "The VPN file $current is missing. Download it from HTB."
  elif [ -n "$refused" ]; then
    set_state error "This VPN file contains commands that could run on your computer, so it was blocked. Download it again from HTB."
  elif ! ensure_kill_switch; then
    set_state offline "No internet connection. Waiting to reconnect."
  else
    if ! vpn_alive; then
      if [ -f "$STATE/hold" ]; then
        set_state offline "VPN paused."
      else
        [ "$vpn_started" -gt 0 ] && restarts=$((restarts + 1))
        start_vpn
      fi
    fi

    gw=$(ip route 2>/dev/null | awk '/ via .* dev tun0/{print $3; exit}')
    if [ -n "$gw" ]; then
      gateway_ms=$(ping -c 1 -W 2 "$gw" 2>/dev/null | sed -n 's/.*time=\([0-9.]*\).*/\1/p')
    else
      gateway_ms=""
    fi

    if [ -n "$gateway_ms" ]; then
      gw_fails=0; ever_connected=yes
      set_state connected "Connected to the lab."
    elif vpn_alive; then
      err=$(classify_error)
      gw_fails=$((gw_fails + 1))
      age=$(( $(date +%s) - vpn_started ))
      network_err=""
      case "$err" in *"reach HTB"*|*"internet connection"*) network_err=yes ;; esac
      if [ -n "$err" ] && ! { [ -n "$ever_connected" ] && [ -n "$network_err" ]; }; then
        # Setup problems always show; network errors only before the file has ever worked.
        set_state error "$err"
      elif [ "$state" = connected ] && [ "$gw_fails" -lt 3 ]; then
        :   # one or two lost pings: not worth alarming anyone
      elif [ "$age" -gt 45 ]; then
        set_state reconnecting "Lost the connection to HTB. Reconnecting..."
      else
        set_state connecting "Connecting to the lab..."
      fi
    fi
  fi

  write_status
  sleep 3
done
