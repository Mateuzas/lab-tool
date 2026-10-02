#!/usr/bin/env python3
"""Type commands into `lab shell` through a pseudo-terminal (for tests).
Usage: python3 tools/drive-shell.py 'cmd 1' 'cmd 2' ...   (an 'exit' is sent at the end)"""
import os
import pty
import select
import sys
import time

pid, fd = pty.fork()
if pid == 0:
    os.execvp("docker", ["docker", "exec", "-it", "kali-lab", "lab-shell"])


def drain(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                return


drain(2)
for line in sys.argv[1:] + ["exit"]:
    os.write(fd, (line + "\r").encode())
    drain(1)
for _ in range(25):  # up to 5 s for the shell to close, then give up
    if os.waitpid(pid, os.WNOHANG)[0]:
        break
    time.sleep(0.2)
else:
    os.kill(pid, 9)
