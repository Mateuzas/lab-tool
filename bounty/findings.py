"""Findings (CONTRACTS §7): drafts in workspace/bounty/findings/<handle>-<slug>/finding.md.

  finding new <slug> [--title T] [--handle H]
  finding list [--handle H] [--json]
  finding evidence <id> FLOW_ID...      redacted request/response text out of the guard's .mitm

Front matter is a flat YAML subset read and written here (no YAML library on the Mac side).
"""

import json
import os
import re
import sys

from bounty import guard, shell
from bounty.util import BountyError, iso, read_json, utcnow, write_bytes

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
FLOW_ID = re.compile(r"^[0-9a-f-]{8,64}$")
SEVERITIES = ("tbd", "none", "low", "medium", "high", "critical")
STATUSES = ("draft", "submitted", "accepted", "duplicate", "informative", "na", "resolved")
KEYS = ("program", "title", "severity", "cvss", "asset", "status", "flows", "ai_used", "created",
        "submitted", "payout_eur")

TEMPLATE = """{front}
## Summary

<!-- One or two sentences: what is wrong, where, and what an attacker gains. -->

## Steps to reproduce

1.
2.
3.

## Impact

<!-- Who is affected and how badly. Be concrete; no worst-case speculation. -->

## Evidence

<!-- `host/bounty finding evidence {id} <flow_id>...` copies redacted requests into evidence/.
     Review them (bodies are not redacted) before attaching. -->

## Suggested fix

## AI usage

<!-- {ai_note} -->
"""


# -- front matter ------------------------------------------------------------------------------

def _scalar(v):
    v = v.strip()
    if v in ("", "null", "~"):
        return None
    if v in ("true", "false"):
        return v == "true"
    if re.match(r"^-?\d+(\.\d+)?$", v):
        return float(v) if "." in v else int(v)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1].replace('\\"', '"') if v[0] == '"' else v[1:-1]
    return v


def _value(v):
    v = v.split(" #", 1)[0].strip() if not v.strip().startswith(("\"", "'")) else v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [] if not inner else [_scalar(x) for x in inner.split(",")]
    return _scalar(v)


def parse(text):
    """finding.md -> (front matter dict, body). Missing front matter -> ({}, text)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    front = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        front[k.strip()] = _value(v)
    return front, text[end + 5:]


def _fmt(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    s = str(v)
    if s == "" or re.search(r"[:#\[\],\"']|^\s|\s$", s) or s in ("null", "true", "false") or re.match(r"^-?\d", s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def dump(front):
    keys = [k for k in KEYS if k in front] + [k for k in front if k not in KEYS]
    return "---\n" + "".join("%s: %s\n" % (k, _fmt(front[k])) for k in keys) + "---\n"


# -- paths -------------------------------------------------------------------------------------

def findings_dir(ws):
    return os.path.join(ws, "findings")


def _handle_ok(h):
    return isinstance(h, str) and re.match(r"^[a-z0-9][a-z0-9-]{0,127}$", h)


def default_handle(handle=None):
    """--handle, else the guard's active program."""
    if handle:
        if not _handle_ok(handle):
            raise BountyError("usage", "bad handle %r" % handle, 64)
        return handle
    cfg = guard._cfg()
    h = shell.active_program(cfg) if guard.running(cfg["container"]) else None
    if not h:
        raise BountyError("usage", "no active program in the guard: pass --handle", 64)
    return h


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# -- commands ----------------------------------------------------------------------------------

def new(ws, slug, title=None, handle=None, out=sys.stdout):
    if not SLUG.match(slug or ""):
        raise BountyError("usage", "slug: lower case letters, digits and -, up to 60 (e.g. idor-shared-folder)", 64)
    h = default_handle(handle)
    profile = read_json(os.path.join(ws, "programs", h, "program.json"))
    if profile is None:
        raise BountyError("usage", "no program %s" % h, 64)
    fid = "%s-%s" % (h, slug)
    d = os.path.join(findings_dir(ws), fid)
    path = os.path.join(d, "finding.md")
    if os.path.exists(path):
        raise BountyError("usage", "%s already exists" % fid, 64)
    ai = ((profile.get("rules") or {}).get("ai_use") or "unknown")
    note = {"allowed-disclosed": "This program allows AI help if you disclose it: say what Claude did.",
            "forbidden": "This program forbids AI use: write this report yourself.",
            }.get(ai, "Say whether and how AI helped (ai_use: %s)." % ai)
    if profile.get("platform") == "manual":
        note += " Manual target: this report goes to the owner."
    front = {"program": h, "title": title or "", "severity": "tbd", "cvss": "", "asset": "",
             "status": "draft", "flows": [], "ai_used": False, "created": iso(utcnow()),
             "submitted": None, "payout_eur": None}
    os.makedirs(os.path.join(d, "evidence"), exist_ok=True)
    write_bytes(path, TEMPLATE.format(front=dump(front), id=fid, ai_note=note).encode("utf-8"))
    out.write("created %s\n" % os.path.relpath(path, ws))
    return 0


_LEGACY = re.compile(r"^- \*\*(Program|Severity|Status|Date):\*\*\s*(.+)$", re.M)


def legacy_front(text):
    """Drafts written before §7's front matter: `# Title` and `- **Program:** …` lines."""
    front = {}
    m = re.search(r"^# (.+)$", text, re.M)
    if m:
        front["title"] = m.group(1).strip()
    for k, v in _LEGACY.findall(text):
        v = v.strip()
        if k == "Program":
            front["program"] = v.split()[0]
        elif k == "Severity":
            w = v.split()[0].lower().strip(".,;:")
            front["severity"] = w if w in SEVERITIES else v[:30]
        elif k == "Status":
            front["status"] = "draft"
            front["note"] = v[:60]
        elif k == "Date":
            front["created"] = v.split()[0]
    front.setdefault("status", "draft")
    return front


def rows(ws, handle=None):
    out = []
    base = findings_dir(ws)
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    for name in names:
        p = os.path.join(base, name, "finding.md")
        if not os.path.isfile(p):
            continue
        text = _read(p)
        front, _ = parse(text)
        if not front:
            front = legacy_front(text)
        if handle and front.get("program") != handle:
            continue
        out.append({"id": name, "program": front.get("program"), "title": front.get("title") or "",
                    "severity": front.get("severity"), "status": front.get("status"),
                    "flows": len(front.get("flows") or []), "created": front.get("created"),
                    "payout_eur": front.get("payout_eur")})
    out.sort(key=lambda r: str(r["created"] or ""), reverse=True)
    return out


def list_cmd(ws, handle=None, as_json=False, out=sys.stdout):
    rs = rows(ws, handle)
    if as_json:
        out.write(json.dumps(rs) + "\n")
        return 0
    if not rs:
        out.write("no findings yet: host/bounty finding new <slug>\n")
        return 0
    for r in rs:
        out.write("%-10s %-8s %-48s %s%s\n" % (r["status"], r["severity"], r["id"][:48], r["title"],
                                                (" (€%s)" % r["payout_eur"]) if r["payout_eur"] else ""))
    return 0


def evidence(ws, fid, flow_ids, out=sys.stdout):
    path = os.path.join(findings_dir(ws), fid or "", "finding.md")
    if not fid or "/" in fid or fid.startswith(".") or not os.path.isfile(path):
        raise BountyError("usage", "no finding %r (finding list shows them)" % fid, 64)
    bad = [i for i in flow_ids if not FLOW_ID.match(i)]
    if not flow_ids or bad:
        raise BountyError("usage", "give flow ids (host/bounty flows --json shows them)%s"
                          % (": bad %s" % ", ".join(bad) if bad else ""), 64)
    text = _read(path)
    front, body = parse(text)
    h = front.get("program")
    if not _handle_ok(h):
        raise BountyError("usage", "%s has no valid program in its front matter" % fid, 64)
    cfg = guard._cfg()
    if not guard.running(cfg["container"]):
        raise BountyError("guard", "%s is not running: start the guard first" % cfg["container"], 3)
    code, stdout, err = guard._docker(["exec", cfg["container"], "bounty-evidence", "--handle", h,
                                       "--ids", ",".join(flow_ids)])
    if code != 0:
        line = next((l for l in reversed(err.splitlines()) if l.startswith("bounty: ")), "")
        raise BountyError("usage" if code == 64 else "evidence", line.split(": ", 2)[-1] or "bounty-evidence failed",
                          64 if code == 64 else 1)
    items = json.loads(stdout or "[]")
    ev = os.path.join(findings_dir(ws), fid, "evidence")
    os.makedirs(ev, exist_ok=True)
    for it in items:
        write_bytes(os.path.join(ev, it["flow_id"] + ".txt"), it["text"].encode("utf-8"))
    flows = list(front.get("flows") or [])
    for it in items:
        if it["flow_id"] not in flows:
            flows.append(it["flow_id"])
    front["flows"] = flows
    write_bytes(path, (dump(front) + body).encode("utf-8"))
    out.write("%d flow(s) -> %s (review them: bodies are not redacted)\n"
              % (len(items), os.path.relpath(ev, ws)))
    return 0
