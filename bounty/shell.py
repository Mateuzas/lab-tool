"""`host/bounty shell [--handle H]` (CONTRACTS §12.1): an interactive login shell in kali-bounty as
uid hunter, through bounty-run, so every request goes through the scope guard.

The active program comes from the guard's own state (the same guard.json `guard status --json`
prints). `--handle` only picks the work dir /bounty/work/<h>; the guard keeps enforcing the active
program's profile whatever the shell is called.
"""

import os

from bounty import guard
from bounty.util import BountyError

WORK = "/bounty/work"


def active_program(cfg):
    """The guard's active handle, or None (guard.json `program`)."""
    st = guard.read_state(cfg) or {}
    h = st.get("program")
    return h if isinstance(h, str) and h else None


def exec_args(container, handle):
    """The docker exec command line of the shell (CONTRACTS §12.1)."""
    return ["docker", "exec", "-it", "-e", "BOUNTY_HANDLE=" + handle, "-w", "%s/%s" % (WORK, handle),
            container, "bounty-run", "--actor", "human", "--", "bash", "-l"]


def mkdir_args(container, handle):
    """Create the work dir as root, owned by hunter (no-op when it exists)."""
    return ["exec", "-u", "root", container, "install", "-d", "-o", "hunter", "-g", "hunter",
            "%s/%s" % (WORK, handle)]


def shell(handle=None, execvp=os.execvp):
    cfg = guard._cfg()
    if not guard.running(cfg["container"]):
        raise BountyError("guard", "%s is not running: start the guard first (host/bounty guard start)"
                          % cfg["container"], 3)
    active = active_program(cfg)
    if not active:
        raise BountyError("guard", "no active program: pick a program with u / program use", 5)
    h = (handle if handle is not None else active).strip().lower()
    if not guard._policy_module().HANDLE_RE.match(h):
        raise BountyError("usage", "%r is not a program handle" % (handle if handle is not None else active), 64)
    guard._docker(mkdir_args(cfg["container"], h), check=True)
    args = exec_args(cfg["container"], h)
    execvp(args[0], args)   # replaces this process; the shell's exit code is ours
    return 0   # only reached with a test's fake execvp
