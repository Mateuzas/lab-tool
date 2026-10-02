#!/usr/bin/env bash
# Command log: drives `lab shell` through a pty and checks workspace/logs/commands.jsonl.
# Needs the container running; no target machine.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
WS=${LAB_WORKSPACE:-workspace}
LOG=$WS/logs/commands.jsonl

before=$(wc -l < "$LOG" 2>/dev/null || echo 0)
python3 tools/drive-shell.py "echo lab-log-test" "" "ls /lab/does-not-exist" 'echo "q" \x' "cd /tmp"
python3 - "$LOG" "$before" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]).readlines()[int(sys.argv[2]):]]
cmds = [(r["cmd"], r["exit"], r["cwd"]) for r in rows]
want = [("echo lab-log-test", 0, "/lab"), ("ls /lab/does-not-exist", 2, "/lab"),
        ('echo "q" \\x', 0, "/lab"), ("cd /tmp", 0, "/tmp")]
ok = cmds == want
print("  logged:", cmds)
print("  PASS: commands, exit codes, folders and escaping logged" if ok else f"  FAIL: expected {want}")
sys.exit(0 if ok else 1)
PY
