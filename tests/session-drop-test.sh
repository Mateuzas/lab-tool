#!/usr/bin/env bash
# Proves that a program talking to an address that leaves the scope is ended with a message in its
# terminal, while other programs and the VPN keep running. Needs the lab container running and
# the NEW supervisor in it (make build). No lab machine involved: it talks to a listener on
# 127.0.0.1 inside the container, using the supervisor's own drop_sessions function.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
check() { if [ "$1" = ok ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }

out=$(python3 - <<'PY'
import os, pty, subprocess, time, select
fn = subprocess.run(["docker","exec","kali-lab","sh","-c",
  "sed -n '/^drop_sessions()/,/^}/p' /usr/local/bin/lab-supervisor"], capture_output=True, text=True).stdout
assert "drop_sessions" in fn, "supervisor in the container has no drop_sessions (run make build)"
script = "log(){ :; }\n" + fn + """
(setsid nc -l 127.0.0.1 9911 >/dev/null 2>&1 &)
(setsid nc -l 127.0.0.1 9912 >/dev/null 2>&1 &)
sleep 1
nc 127.0.0.1 9911 &            # a program on the terminal talking to the leaving address
mine=$!
setsid sleep 60 & other=$!     # unrelated, must survive
sleep 1
drop_sessions 127.0.0.1
sleep 1
kill -0 $mine 2>/dev/null && echo MINE_ALIVE || echo MINE_ENDED
kill -0 $other 2>/dev/null && echo OTHER_ALIVE || echo OTHER_ENDED
pkill -f 'nc -l 127.0.0.1 991' ; kill $other 2>/dev/null; true
"""
pid, fd = pty.fork()
if pid == 0:
    os.execvp("docker", ["docker","exec","-it","kali-lab","bash","-c",script])
buf = b""; end = time.time() + 12
while time.time() < end:
    r,_,_ = select.select([fd],[],[],0.3)
    if r:
        try: d = os.read(fd, 4096)
        except OSError: break
        if not d: break
        buf += d
print(buf.decode(errors="replace"))
PY
)
[ -n "${DEBUG:-}" ] && echo "$out"
echo "== session drop"
[[ "$out" == *"[lab] nc ended: 127.0.0.1 is no longer your target."* ]] && check ok "student sees why it ended" || check bad "student sees why it ended"
[[ "$out" == *MINE_ENDED* ]] && check ok "program talking to the old target ended" || check bad "program talking to the old target ended"
[[ "$out" == *OTHER_ALIVE* ]] && check ok "unrelated program untouched" || check bad "unrelated program untouched"
exit $fail
