"""Manual targets: a program profile for a system you were authorised to test directly, outside
any platform (CONTRACTS §2, legal_basis "authorization"; ADR 010).

The guard enforces a manual profile exactly like an Intigriti one: it only needs a handle, a scope
with at least one enforceable in-scope entry, and a rate limit. `sync`, `rank`, `changes` and the
notifier ignore it (platform != "intigriti"), so it never reaches the Intigriti API.

Only create one for a system whose owner has authorised the testing in writing. The authorisation
details and the path to that written permission are recorded in the profile.
"""

import ipaddress
import os
import re

from bounty.util import BountyError, iso, read_json, utcnow, write_bytes
import json


def _now():
    return iso(utcnow())

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(name):
    s = _SLUG.sub("-", (name or "").lower()).strip("-")
    return s[:100] or "target"


def classify(value):
    """A raw scope string -> a scope entry dict (type inferred). Mirrors what the guard enforces."""
    v = (value or "").strip()
    if not v:
        raise BountyError("usage", "empty scope entry", 64)
    # CIDR, e.g. 10.0.0.0/24
    if "/" in v and v.split("/")[-1].isdigit() and ":" not in v.split("/")[0].strip("[]"):
        try:
            ipaddress.ip_network(v, strict=False)
            return {"type": "cidr", "value": v, "tier": "tier-1", "description": ""}
        except ValueError:
            pass
    low = v.lower()
    if "://" in low or (("/" in v) and not v.startswith("*.")):
        return {"type": "url", "value": v, "tier": "tier-1", "description": ""}
    if v.startswith("*."):
        return {"type": "wildcard", "value": low, "tier": "tier-1", "description": ""}
    # a bare IP is a /32 (or /128) so the guard treats it as an address, not a hostname
    try:
        ip = ipaddress.ip_address(v)
        return {"type": "cidr", "value": "%s/%d" % (ip, 32 if ip.version == 4 else 128),
                "tier": "tier-1", "description": ""}
    except ValueError:
        pass
    return {"type": "domain", "value": low, "tier": "tier-1", "description": ""}


def build_profile(name, scope_in, scope_out, rate_rps, request_header, authorized_by,
                  note_path, expires):
    now = _now()
    if not scope_in:
        raise BountyError("usage", "a manual target needs at least one --scope entry", 64)
    rules = {
        "version": None, "changed_at": now, "safe_harbour": None,
        "rate_limit_rps": rate_rps,
        "automation": "allowed" if (rate_rps is None or rate_rps > 0) else "forbidden",
        "intigriti_me": False, "user_agent": None,
        "request_header": request_header, "ai_use": "unknown",
        "text": "Manual target. Authorised by: %s." % (authorized_by or "(unrecorded)"),
    }
    return {
        "schema": 1, "handle": "manual-" + slug(name), "platform": "manual",
        "name": name, "url": "", "status": "open", "detail": "ok", "synced_at": now,
        "bounty": {"min": None, "max": None, "currency": None},
        "scope": {"version": None, "changed_at": now,
                  "in": [classify(s) for s in scope_in],
                  "out": [classify(s) for s in scope_out]},
        "rules": rules, "legal_basis": "authorization",
        "authorization": {"owner": authorized_by, "note_path": note_path,
                          "granted_at": now, "expires": expires},
    }


def add(ws, args, out):
    """Write workspace/bounty/programs/manual-<slug>/program.json from the CLI args."""
    header = None
    if args.header:
        if ":" not in args.header:
            raise BountyError("usage", "--header must be 'Name: value'", 64)
        header = args.header.strip()
    note_path = None
    if args.auth_note:
        note_path = os.path.abspath(os.path.expanduser(args.auth_note))
        if not os.path.isfile(note_path):
            raise BountyError("usage", "--auth-note %s does not exist" % args.auth_note, 64)
    profile = build_profile(args.name, args.scope or [], args.out or [], args.rate,
                            header, args.authorized_by, note_path, args.expires)
    # Validate against the real guard policy before writing, so `program use` can't later fail.
    _validate(profile)
    h = profile["handle"]
    d = os.path.join(ws, "programs", h)
    path = os.path.join(d, "program.json")
    if os.path.exists(path) and not args.force:
        raise BountyError("usage", "%s already exists (use --force to overwrite)" % h, 64)
    os.makedirs(d, exist_ok=True)
    write_bytes(path, (json.dumps(profile, indent=2, sort_keys=True) + "\n").encode())
    ni = len(profile["scope"]["in"])
    no = len(profile["scope"]["out"])
    out.write("created %s: %d in-scope / %d out-of-scope entries%s\n"
              % (h, ni, no, "" if args.authorized_by else " (no --authorized-by recorded)"))
    out.write("start it with: host/bounty guard start && host/bounty program use %s\n" % h)
    if not note_path:
        out.write("tip: keep the owner's written authorisation and pass it with --auth-note\n")
    return 0


def _validate(profile):
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(root, "container", "share", "bounty", "guard_policy.py")
    spec = importlib.util.spec_from_file_location("guard_policy", p)
    gp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gp)
    try:
        gp.Policy(profile, "check", None, profile["handle"])
    except gp.PolicyError as e:
        raise BountyError("usage", "this scope can't be enforced: %s" % e.message, 64)
