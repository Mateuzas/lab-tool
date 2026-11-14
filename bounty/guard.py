"""Mac side of the scope guard (CONTRACTS §3.3, §6): `guard start|stop|status`, `program use`.

The container is controlled only with `docker run/exec/rm` (never by editing shared files: Docker
Desktop can serve stale copies). INTIGRITI_USERNAME reaches the guard through `docker exec -e`
(name only on the command line, value from this process's environment) and stays in tmpfs there.

Environment (tests use them so they never touch the real containers):
  BOUNTY_CONTAINER (kali-bounty), BOUNTY_LAB_CONTAINER (kali-lab), BOUNTY_IMAGE (kali-lab:dev),
  BOUNTY_GUARD_PORT (8081, the Mac port Burp uses as upstream proxy).
"""

import json
import os
import subprocess
import sys
import time

from bounty.util import REPO_ROOT, BountyError, read_json

USERNAME_ENV = "INTIGRITI_USERNAME"


def _cfg():
    e = os.environ
    port = e.get("BOUNTY_GUARD_PORT") or "8081"
    if not port.isdigit():
        raise BountyError("usage", "BOUNTY_GUARD_PORT must be a port number", 64)
    return {"container": e.get("BOUNTY_CONTAINER") or "kali-bounty",
            "lab": e.get("BOUNTY_LAB_CONTAINER") or "kali-lab",
            "image": e.get("BOUNTY_IMAGE") or "kali-lab:dev",
            "port": port}


def _docker(args, check=False, capture=True, env=None, input_text=None):
    try:
        r = subprocess.run(["docker"] + args, stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if capture else None, env=env,
                           input=input_text.encode() if input_text is not None else None)
    except FileNotFoundError:
        raise BountyError("docker", "docker is not installed or not on PATH", 1)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    if check and r.returncode != 0:
        raise BountyError("docker", (err.strip().splitlines() or ["docker %s failed" % args[0]])[-1], 1)
    return r.returncode, out, err


def running(name):
    code, out, _ = _docker(["inspect", "-f", "{{.State.Running}}", name])
    return code == 0 and out.strip() == "true"


def exists(name):
    return _docker(["inspect", name])[0] == 0


def read_state(cfg):
    code, out, _ = _docker(["exec", cfg["container"], "cat", "/bounty/state/guard.json"])
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def run_args(cfg, ws):
    """CONTRACTS §3.3, exactly (name, image and Mac port configurable for tests)."""
    return ["run", "-d", "--name", cfg["container"], "--init", "--restart", "unless-stopped",
            "--cap-add=NET_ADMIN", "-p", "127.0.0.1:%s:8080" % cfg["port"], "-v", "%s:/bounty" % ws,
            cfg["image"], "bounty-supervisor"]


def guard_start(ws, out=sys.stdout):
    cfg = _cfg()
    if running(cfg["lab"]):
        _docker(["stop", cfg["lab"]], check=True)
        out.write("stopped %s: one mode at a time (Lab comes back with host/lab up)\n" % cfg["lab"])
    os.makedirs(ws, exist_ok=True)
    if exists(cfg["container"]):
        _docker(["start", cfg["container"]], check=True)
    else:
        _docker(run_args(cfg, ws), check=True)
    st = None
    end = time.time() + 30
    while time.time() < end:
        st = read_state(cfg)
        if st and st.get("guard") == "up" and st.get("status") not in ("starting", "down"):
            break
        time.sleep(0.5)
    if not st or st.get("guard") != "up":
        why = (st or {}).get("last_error") or "no guard.json after 30 s"
        raise BountyError("guard", "%s started but the guard is not up: %s" % (cfg["container"], why), 1)
    out.write("guard up in %s: proxy 127.0.0.1:%s (Burp upstream proxy), firewall %s, status %s\n"
              % (cfg["container"], cfg["port"], st.get("firewall"), st.get("status")))
    if st.get("status") == "no-profile":
        out.write("no active program: everything is refused until host/bounty program use <handle>\n")
    return 0


def guard_stop(out=sys.stdout):
    cfg = _cfg()
    if not exists(cfg["container"]):
        out.write("%s is not there\n" % cfg["container"])
        return 0
    _docker(["rm", "-f", cfg["container"]], check=True)
    out.write("removed %s (workspace kept)\n" % cfg["container"])
    return 0


def guard_status(as_json=False, out=sys.stdout):
    cfg = _cfg()
    st = read_state(cfg) if running(cfg["container"]) else None
    if as_json:
        out.write(json.dumps(st if st else {"status": "not-running"}, indent=1) + "\n")
        return 0
    if not st:
        out.write("guard: not running (host/bounty guard start)\n")
        return 0
    out.write("guard: %s (firewall %s, process %s, restarts %s)\n"
              % (st.get("status"), st.get("firewall"), st.get("guard"), st.get("guard_restarts")))
    out.write("program: %s\n" % (st.get("program") or "none"))
    if st.get("effective_rps") is not None:
        out.write("rate: %g req/s (profile rate_limit_rps: %s), automation %s\n"
                  % (st["effective_rps"], st.get("rate_limit_rps"), st.get("automation")))
    ident = st.get("identification") or {}
    if ident.get("request_header") or ident.get("user_agent"):
        out.write("identification: %s\n" % ", ".join(
            x for x in [ident.get("request_header") and "header " + ident["request_header"],
                        ident.get("user_agent") and "User-Agent template"] if x))
    if st.get("egress_ip") or st.get("egress_error"):
        out.write("egress ip: %s\n" % (st.get("egress_ip") or "unknown (%s)" % st.get("egress_error")))
    if st.get("last_error"):
        out.write("last error: %s\n" % st["last_error"])
    refusals = st.get("last_refusals") or []
    out.write("last refusals: %s\n" % ("none" if not refusals else ""))
    for r in refusals:
        out.write("  %s %-20s %-12s %s %s\n" % (r.get("ts"), r.get("reason"), r.get("actor"), r.get("method"),
                                                 r.get("url")))
    return 0


def _policy_module():
    """The guard's own decision module (container/share/bounty/guard_policy.py), so the Mac checks
    a profile with exactly the code the guard runs."""
    path = os.path.join(REPO_ROOT, "container", "share", "bounty")
    if path not in sys.path:
        sys.path.insert(0, path)
    import guard_policy
    return guard_policy


def program_use(ws, handle, out=sys.stdout):
    gp = _policy_module()
    h = (handle or "").strip().lower()
    if not gp.HANDLE_RE.match(h):
        raise BountyError("usage", "%r is not a program handle" % handle, 64)
    def load(handle_):
        path = os.path.join(ws, "programs", handle_, "program.json")
        try:
            return read_json(path)
        except ValueError as e:
            raise BountyError("parse", "%s is not valid JSON (%s): not activated" % (path, e), 4)
    profile = load(h)
    if profile is None and not h.startswith("intigriti-"):
        alt = load("intigriti-" + h)
        if alt is not None:
            h, profile = "intigriti-" + h, alt
    if profile is None:
        raise BountyError("usage", "no synced program %s (programs list)" % h, 64)
    username = os.environ.get(USERNAME_ENV)
    try:   # templates and scope first: never activate a profile the guard cannot enforce
        gp.Policy(profile, username or "check", None, h)
    except gp.PolicyError as e:
        raise BountyError("parse" if e.exit_code == 4 else "auth", "%s: %s" % (h, e.message), e.exit_code)
    cfg = _cfg()
    if not running(cfg["container"]):
        raise BountyError("guard", "%s is not running (host/bounty guard start)" % cfg["container"], 5)
    code, _, err = _docker(["exec", "-i", cfg["container"], "sh", "-c",
                            "cat > /bounty/.active.tmp && mv /bounty/.active.tmp /bounty/active"],
                           input_text=h + "\n")
    if code != 0:
        raise BountyError("guard", "could not write /bounty/active: %s" % err.strip(), 1)
    args = ["exec"]
    if username:
        args += ["-e", USERNAME_ENV]   # value comes from our environment, not the command line
    args += [cfg["container"], "bounty-ctl", "reload"]
    code, o, e = _docker(args)
    for line in (o + e).splitlines():
        out.write(line.replace("bounty-ctl: ", "") + "\n")
    if code == 0:
        out.write("active program: %s\n" % h)
        return 0
    if code in (2, 4):
        raise BountyError("auth" if code == 2 else "parse", "%s was not activated; the guard refuses everything" % h,
                          code)
    raise BountyError("guard", "bounty-ctl reload failed (exit %d)" % code, 1)
