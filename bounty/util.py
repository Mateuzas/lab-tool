"""Small shared helpers: workspace paths, timestamps, atomic JSON files."""

import datetime
import json
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BountyError(Exception):
    """An error with a CLI exit code and a short code word (CONTRACTS §6: `bounty: <code>: <msg>`)."""

    def __init__(self, code, message, exit_code):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def workspace_dir(env=None):
    """BOUNTY_WORKSPACE, default <repo>/workspace/bounty."""
    env = os.environ if env is None else env
    path = env.get("BOUNTY_WORKSPACE") or os.path.join(REPO_ROOT, "workspace", "bounty")
    return os.path.abspath(os.path.expanduser(path))


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def ts_name(dt):
    """2026-11-13T12:00:00Z -> 20261113T120000Z (file and folder names)."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_unix(seconds):
    if seconds is None:
        return None
    return iso(datetime.datetime.fromtimestamp(int(seconds), datetime.timezone.utc))


def parse_time(text):
    """Accepts YYYYMMDDTHHMMSSZ, ISO 8601 with Z/offset, or YYYY-MM-DD. Returns aware UTC datetime."""
    if text is None:
        return None
    text = text.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BountyError("usage", "bad timestamp %r (use YYYYMMDDTHHMMSSZ or ISO 8601)" % text, 64)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def write_bytes(path, data):
    """Atomic write (temp file + rename) so an interrupted sync never leaves half a file."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dumps(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_json(path, obj):
    write_bytes(path, dumps(obj).encode("utf-8"))


def read_json(path, default=None):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        return default


def append_jsonl(path, records):
    if not records:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except FileNotFoundError:
        pass
    return out
