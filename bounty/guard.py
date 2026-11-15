"""Mac side of the scope guard (CONTRACTS §3.3, §6): `guard start|stop|status`, `program use`.

The container is controlled only with `docker run/exec/rm` (never by editing shared files: Docker
Desktop can serve stale copies). INTIGRITI_USERNAME reaches the guard through `docker exec -e`
(name only on the command line, value from this process's environment) and stays in tmpfs there.

Mounts: the workspace read-only at /bounty (this Mac side is the only writer of profiles and
`active`), <workspace>/work read-write at /bounty/work (hunter's output), and the Docker volume
kali-bounty-guard at /var/lib/bounty-guard (guard state, flows, CA), read here only via docker exec.

Environment (tests use them so they never touch the real containers):
  BOUNTY_CONTAINER (kali-bounty), BOUNTY_LAB_CONTAINER (kali-lab), BOUNTY_IMAGE (kali-lab:dev),
  BOUNTY_GUARD_PORT (8081, the Mac port Burp uses as upstream proxy),
  BOUNTY_GUARD_VOLUME (kali-bounty-guard), BOUNTY_HOME_VOLUME (kali-bounty-home),
  BOUNTY_GUARD_ADD_HOSTS (tests only: "name:ip name:ip", written to /etc/hosts at creation, since
  the read-only root filesystem keeps /etc/hosts read-only afterwards).

Hardening (CONTRACTS §3.3): every capability dropped except the ones the supervisor needs as root
(firewall, switching to guard/hunter, preparing their directories, killing a stray guard),
no-new-privileges, a read-only root filesystem with tmpfs for the paths written at run time (the
trust store is rebuilt there on every start), and hunter's home in its own volume. A container made
with an older spec (label kali-lab.guard-spec) is recreated by `guard start`; the volumes stay.
"""

import datetime
import json
import os
import subprocess
import sys
import time

from bounty.util import REPO_ROOT, BountyError, read_json, write_bytes

USERNAME_ENV = "INTIGRITI_USERNAME"
VAR = "/var/lib/bounty-guard"
SPEC = "2"   # bump whenever run_args changes, so `guard start` recreates an older container
CAPS = ("NET_ADMIN", "SETUID", "SETGID", "SETPCAP", "CHOWN", "FOWNER", "DAC_OVERRIDE", "KILL")
TMPFS = (("/tmp", "1777"), ("/run", "755"), ("/var/log", "755"), ("/etc/ssl/certs", "755"),
         ("/usr/local/share/ca-certificates", "755"))


def _cfg():
    e = os.environ
    port = e.get("BOUNTY_GUARD_PORT") or "8081"
    if not port.isdigit():
        raise BountyError("usage", "BOUNTY_GUARD_PORT must be a port number", 64)
    return {"container": e.get("BOUNTY_CONTAINER") or "kali-bounty",
            "lab": e.get("BOUNTY_LAB_CONTAINER") or "kali-lab",
            "image": e.get("BOUNTY_IMAGE") or "kali-lab:dev",
            "port": port,
            "volume": e.get("BOUNTY_GUARD_VOLUME") or "kali-bounty-guard",
            "home": e.get("BOUNTY_HOME_VOLUME") or "kali-bounty-home",
            "add_hosts": (e.get("BOUNTY_GUARD_ADD_HOSTS") or "").split()}


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
    code, out, _ = _docker(["exec", cfg["container"], "cat", VAR + "/guard.json"])
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def run_args(cfg, ws):
    """CONTRACTS §3.3 (name, image, Mac port and volume configurable for tests). Only the guard's
    8080 is published, on 127.0.0.1; the claude port 8082 stays inside the container."""
    args = ["run", "-d", "--name", cfg["container"], "--init", "--restart", "unless-stopped",
            "--label", "kali-lab.guard-spec=" + SPEC, "--cap-drop=ALL"]
    args += ["--cap-add=" + c for c in CAPS]
    args += ["--security-opt", "no-new-privileges:true", "--read-only"]
    for path, mode in TMPFS:
        args += ["--tmpfs", "%s:rw,nosuid,nodev,mode=%s" % (path, mode)]
    for entry in cfg.get("add_hosts") or ():
        args += ["--add-host", entry]
    args += ["-p", "127.0.0.1:%s:8080" % cfg["port"],
             "-v", "%s:/bounty:ro" % ws, "-v", "%s:/bounty/work:rw" % os.path.join(ws, "work"),
             "-v", "%s:%s" % (cfg["volume"], VAR), "-v", "%s:/home/hunter" % cfg["home"],
             cfg["image"], "bounty-supervisor"]
    return args


def spec_of(name):
    code, out, _ = _docker(["inspect", "-f", '{{index .Config.Labels "kali-lab.guard-spec"}}', name])
    return out.strip() if code == 0 else None


def guard_start(ws, out=sys.stdout):
    cfg = _cfg()
    if running(cfg["lab"]):
        _docker(["stop", cfg["lab"]], check=True)
        out.write("stopped %s: one mode at a time (Lab comes back with host/lab up)\n" % cfg["lab"])
    os.makedirs(os.path.join(ws, "work"), exist_ok=True)
    if exists(cfg["container"]) and spec_of(cfg["container"]) != SPEC:
        _docker(["rm", "-f", cfg["container"]], check=True)
        out.write("recreated %s with the current hardening (flows, CA and hunter's home kept)\n"
                  % cfg["container"])
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


def guard_stop(purge=False, yes=False, out=sys.stdout):
    cfg = _cfg()
    if purge and not yes:
        raise BountyError("usage", "--purge deletes the volume %s: every flow log (report evidence) and the "
                                   "guard CA (and %s, hunter's home). Export flows first (flows export), then add --yes" % (cfg["volume"], cfg["home"]), 64)
    if exists(cfg["container"]):
        _docker(["rm", "-f", cfg["container"]], check=True)
        out.write("removed %s (workspace kept)\n" % cfg["container"])
    else:
        out.write("%s is not there\n" % cfg["container"])
    if purge:
        for vol, what in ((cfg["volume"], "flows and guard CA"), (cfg["home"], "hunter's home")):
            if _docker(["volume", "inspect", vol])[0] == 0:
                _docker(["volume", "rm", vol], check=True)
                out.write("removed volume %s (%s)\n" % (vol, what))
    else:
        out.write("flows and guard CA kept in volume %s\n" % cfg["volume"])
    return 0


def _flow_handle(cfg, handle):
    gp = _policy_module()
    if handle is None:
        st = read_state(cfg) or {}
        handle = st.get("program")
        if not handle:
            raise BountyError("usage", "no active program: pass --handle", 64)
    h = handle.strip().lower()
    if h != "_none" and not gp.HANDLE_RE.match(h):
        raise BountyError("usage", "%r is not a program handle" % handle, 64)
    return h


def flows(handle=None, tail=50, as_json=False, out=sys.stdout):
    """The guard's flow log of one program (CONTRACTS §5), read with docker exec."""
    cfg = _cfg()
    if not running(cfg["container"]):
        raise BountyError("guard", "%s is not running (host/bounty guard start)" % cfg["container"], 5)
    h = _flow_handle(cfg, handle)
    if tail is not None and tail < 1:
        raise BountyError("usage", "--tail must be >= 1", 64)
    script = 'cat "$1"/*.jsonl 2>/dev/null' + (' | tail -n %d' % tail if tail else "")
    code, text, err = _docker(["exec", cfg["container"], "sh", "-c", script, "sh", "%s/flows/%s" % (VAR, h)])
    recs = []
    for line in text.splitlines():
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    if as_json:
        out.write(json.dumps(recs, indent=1, ensure_ascii=False) + "\n")
        return 0
    for r in recs:
        out.write("%s %-12s %-8s %-20s %-6s %3s %s\n" % (
            r.get("ts"), r.get("actor"), r.get("decision"), r.get("reason") or "-", r.get("method"),
            r.get("status") if r.get("status") is not None else "-", r.get("url")))
    if not recs:
        out.write("no flows for %s\n" % h)
    return 0


def flows_export(handle, dest, out=sys.stdout):
    """docker cp of one program's flows (jsonl + mitm) into <dest>/<handle>-flows-<UTC ts>/."""
    cfg = _cfg()
    if not exists(cfg["container"]):
        raise BountyError("guard", "%s does not exist (host/bounty guard start)" % cfg["container"], 5)
    h = _flow_handle(cfg, handle)
    dest = os.path.abspath(os.path.expanduser(dest))
    os.makedirs(dest, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = os.path.join(dest, "%s-flows-%s" % (h, stamp))
    code, _, err = _docker(["cp", "%s:%s/flows/%s" % (cfg["container"], VAR, h), target])
    if code != 0:
        raise BountyError("guard", "no flows for %s (%s)" % (h, err.strip()), 1)
    out.write("exported flows of %s to %s\n" % (h, target))
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
    # The Mac is the only writer of `active` (the container sees /bounty read-only); bounty-ctl
    # waits until the shared folder shows the new value.
    write_bytes(os.path.join(ws, "active"), (h + "\n").encode())
    args = ["exec"]
    if username:
        args += ["-e", USERNAME_ENV]   # value comes from our environment, not the command line
    args += [cfg["container"], "bounty-ctl", "reload", "--expect", h]
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
