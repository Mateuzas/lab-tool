# Contracts

Only the Architect edits this file. If code and contract disagree, report it; don't "fix" either side
alone. §2–§4 are based on the real API (docs/research/intigriti-api.md); §7 is still a draft.

## 1. Workspace layout (Bug bounty)

```
workspace/
  secrets/                       SOPS-encrypted user secrets (never mounted into any container)
    intigriti.enc.yaml             INTIGRITI_TOKEN (and later INTIGRITI_USERNAME)
  bounty/                        mounted into kali-bounty at /bounty (and nothing else is)
    config/
      ranking.json                 ranking weights (§4); a default is written on first run
      recon-sources.json           passive recon allow-list (§3.4)
    active                         one line: the handle of the active program, or empty
    sync/
      <UTC timestamp>.json         index of one sync: handles, counts, errors
    activities.jsonl             every program activity seen (deduplicated), for first_seen_at and changes
    programs/<handle>/
      raw/<UTC timestamp>/*.json   API responses exactly as received
      program.json                 normalised profile (§2)
      history.jsonl                one line per change found by `programs changes` (§6)
      notes.md                     free text, the user's
      findings/<id>/finding.md     a finding (§7), with evidence files beside it
      time.jsonl                   hours spent: {"start","end","note"} (feeds ranking feedback)
    work/<handle>/               the ONLY folder kali-bounty can write to (tool output, scratch)
      recon/                       one file per run: <tool>-<UTC timestamp>.<ext>
```

Inside `kali-bounty` the workspace is **read-only** (`/bounty:ro`), except `work/` (`/bounty/work:rw`).
Docker Desktop ignores Unix permissions on bind mounts, so a writable `/bounty` would let any tool
(or Claude) edit a profile and widen the scope. Guard state and evidence live in the Docker volume
`kali-bounty-guard` at `/var/lib/bounty-guard/` (permissions enforced: root:guard, 0750):

```
/var/lib/bounty-guard/
  guard.json                     status, program, rate, versions, restarts, egress_ip, last refusals, reloads
  guard-events.log               reloads, restarts, firewall re-locks
  ca/                            the guard's CA; the key is readable only by uid guard
  flows/<handle>/<YYYYMMDD>.jsonl  flow log (§5), and .mitm full flows for evidence
```

The Mac reads it with `docker exec` (`host/bounty guard status`, `host/bounty flows`) and copies
evidence out with `host/bounty flows export <handle> <dir>`. The volume survives `guard stop`;
`guard stop --purge` removes it.

`<handle>` is the platform handle, lower case, `[a-z0-9-]`, prefixed with the platform:
`intigriti-<api handle>`. When API handles collide (several programs are called `vdp`) it is
`intigriti-<company>-<api handle>`, company from `webLinks`. Once assigned, a handle stays fixed by
`platform_id`. `&` → `-and-`, accents dropped.

The data folder is `$BOUNTY_WORKSPACE`, default `<repo>/workspace/bounty` (worktrees point it at the
main clone's data). Timestamps: UTC, `YYYYMMDDTHHMMSSZ` in names, ISO 8601 in JSON.

## 2. Program profile: `program.json`

Built from the API (docs/research/intigriti-api.md). `raw/` keeps the responses it came from.

```json
{
  "schema": 1,
  "handle": "intigriti-acme",                 // "intigriti-" + API handle, lower case
  "platform": "intigriti",
  "platform_id": "0083a5bf-…",
  "name": "Acme",
  "url": "https://app.intigriti.com/…/detail",  // webLinks.detail
  "status": "open",                          // open | suspended | closed  (status.value, lower case)
  "confidentiality": "public",               // public | registered | application
  "industry": "Software",
  "detail": "ok",                            // ok | needs-join (detail 403) | error | not-fetched (program not open; scope/rules from an earlier sync are kept)
  "synced_at": "2026-11-13T12:00:00Z",
  "first_seen_at": "2025-01-10T08:00:00Z",   // earliest activity we have for it; null if none
  "bounty": {"min": 50, "max": 10000, "currency": "EUR", "min_eur": 50, "max_eur": 10000},
  "scope": {                                 // null when detail != ok
    "version": "<domains.id>",
    "changed_at": "2026-10-01T09:00:00Z",    // domains.createdAt
    "in":  [{"type": "wildcard", "value": "*.acme.com", "tier": "tier-1", "description": ""}],
    "out": [{"type": "url", "value": "https://status.acme.com", "description": ""}]
  },
  "rules": {                                 // null when detail != ok
    "version": "<rulesOfEngagement.id>",
    "changed_at": "2026-10-01T09:00:00Z",
    "safe_harbour": true,
    "rate_limit_rps": 5,                     // automatedTooling; null = not stated
    "automation": "allowed",                 // allowed (rps > 0) | forbidden (rps == 0) | unknown (null)
    "intigriti_me": true,                    // test accounts must use your @intigriti.me address
    "user_agent": null,                      // required User-Agent template, or null
    "request_header": "X-Intigriti-Username: {Username}",   // template, or null
    "ai_use": "allowed-disclosed",           // platform Code of Conduct default; "forbidden" if the rules text says so (manual tag)
    "text": "<rules of engagement markdown>"
  },
  "legal_basis": "program"                   // program | lt-article-25 (ADR 008)
}
```

Mapping: API `type` → `wildcard` (Wildcard), `url` (Url; a bare host becomes `domain`), `cidr`
(IpRange), `android`, `ios`, `source`, `device`, `other`. API `tier` → `tier-1`…`tier-5`,
`no-bounty`; entries whose tier is **Out Of Scope go to `scope.out`**, everything else to `scope.in`.
Currency → EUR with fixed rates from `config/ranking.json` (`fx`), not live rates.

Enforceable by the guard: `wildcard` (`*.example.com` matches subdomains at any depth, **not** the
apex), `domain` (exact host), `url` (scheme + host + path prefix), `cidr`. The rest are shown only.
`{Username}` in templates is replaced with `INTIGRITI_USERNAME` at `program use` time.

## 3. Scope guard

### 3.1 Decision
For each request the guard computes `host` from the CONNECT target, the TLS SNI and the `Host`
header. **All three must agree** (case-insensitive, trailing dot removed), otherwise
`refused: host-mismatch`. Then, in order:

1. no active profile → `no-profile`
2. host matches any `scope.out` entry → `out-of-scope`
3. host is in `recon-sources.json` → allow, category `recon-source` (no header, own rate limit)
4. no `scope.in` entry matches → `not-in-scope`
5. a resolved IP is private, loopback, link-local, CGNAT or metadata (169.254.169.254) and the
   profile doesn't list it as `ip`/`cidr` → `internal-address`
6. rate limit bucket empty → wait up to 10 s, then `rate-limited`. Rate = `rules.rate_limit_rps`;
   null → 2 req/s; 0 → only `--actor human` traffic (Burp, your terminal), tools and Claude refused
   with `automation-forbidden`
7. otherwise allow, apply the program's identification, forward:
   - `request_header` template → that header (e.g. `X-Intigriti-Username: mateuzas`), replacing any
     existing one;
   - `user_agent` template → the `User-Agent` becomes the template with the username filled in and
     the client's original User-Agent in the `<user agent>` slot (Dropbox:
     `Intigriti - mateuzas - Mozilla/5.0 …`); a missing original becomes `kali-lab`.
   Placeholders are matched case-insensitively in both spellings platforms use: `{Username}`,
   `<username>`, and `<user agent>` / `{UserAgent}`. A template with an unknown placeholder makes
   `program use` fail (exit 4) rather than send a malformed header.

`url` entries also check the path prefix (after the request line is known).

### 3.2 Refusal response
`HTTP/1.1 403 Forbidden`, header `X-Bounty-Guard: refused; reason=<code>`, body one line of text.
Reason codes: `no-profile`, `host-mismatch`, `out-of-scope`, `not-in-scope`, `internal-address`,
`rate-limited`, `automation-forbidden`.

### 3.3 Ports and processes
| What | Where |
|---|---|
| guard listen | `0.0.0.0:8080` in `kali-bounty` (human traffic), published only as `127.0.0.1:8081` on the Mac; `127.0.0.1:8082` inside the container for `--actor claude` (not published) |
| guard user | uid `guard` (only uid allowed new outbound connections) |
| tool user (human) | uid `hunter`; env `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` = `http://127.0.0.1:8080` |
| guard CA | generated at first start in `/var/lib/bounty-guard/ca/`; only the certificate goes into the container's trust store; Burp is told not to validate the upstream |
| control | `docker exec kali-bounty bounty-ctl reload` re-reads `/bounty/active` and the profile |
| container | `docker run -d --name kali-bounty --init --restart unless-stopped --cap-add=NET_ADMIN -p 127.0.0.1:8081:8080 -v $BOUNTY_WORKSPACE:/bounty:ro -v $BOUNTY_WORKSPACE/work:/bounty/work:rw -v kali-bounty-guard:/var/lib/bounty-guard kali-lab:dev bounty-supervisor` (no `/dev/net/tun`, no VPN, no other mount); started by `host/bounty guard start`, stopped by `guard stop`; starting it stops `kali-lab` and vice versa (one mode at a time) |
| users | `guard` uid 990 (system, no shell), `hunter` uid 1000 (home `/home/hunter`); both created in the image |

### 3.4 `recon-sources.json`
`{"hosts": ["crt.sh", "web.archive.org", ...], "rate_limit_rps": 2}`. Exact hosts only, no wildcards.

## 4. Ranking (ADR 007)

`host/bounty programs rank [--json] [--top N] [--explain <handle>] [--include-no-bounty] [--include-join]`

Only `status: open` programs. Each gets four factors in `0..1`; `pr(x)` is the mid-rank percentile
of `x` among the ranked (not excluded) programs, ties share the average position, one program
scores 0.5; missing inputs are dropped and the remaining weights inside
the factor renormalised (never guessed).

| Factor | Formula (v1) |
|---|---|
| `payout` | `pr(0.6·ln(1+max_eur) + 0.4·ln(1+min_eur)) · (0.5 + 0.5·paid_share)`; `paid_share` = in-scope entries in tiers 1–5 ÷ all in-scope entries |
| `findability` | `0.7·pr(ln(1+surface)) + 0.3·freshness`; `surface` = 5·wildcards + 2·cidr + 1·(url+domain) + 0.5·(android+ios+source); `freshness` = `exp(−days_since(scope.changed_at)/90)` |
| `competition` | `0.5·conf + 0.25·age + 0.25·pr(max_eur)`; `conf` public 1.0, registered 0.7, application 0.4; `age` = `min(1, days_since(first_seen_at)/730)` |
| `friction` | sum, capped at 1: automation forbidden 0.6, rps ≤ 2 0.3, rps unknown 0.15, `intigriti_me` 0.05, `user_agent` 0.05 |

```
total = w.payout·payout + w.findability·findability + w.competition·(1 − competition) − w.friction·friction
```

Hard filters (shown in a separate "excluded" list with the reason): `max_eur == 0` (`no-bounty`,
unless `--include-no-bounty`), `safe_harbour == false` (`no-safe-harbour`), no enforceable web entry
in scope (`no-web-scope`). Programs without detail (`needs-join`, `error`) are ranked on list fields: payout without
`paid_share`, competition, and **assumed** findability 0.5 and friction 0.15 (shown in `--explain`).
They are marked `join` and listed after the others unless `--include-join` mixes them in.
A "web entry" is `wildcard`, `domain` or `url`. In `--json`, excluded programs have `total` and
factors `null`.

`--json`: `[{"handle","total","factors":{"payout","findability","competition","friction"},"inputs":{…},"flags":["join"],"excluded":null|"<reason>"}]`, sorted by `total` desc.
Text: one line per program: rank, total, the four factors, max bounty, surface, days since scope
change, confidentiality, flags.

`config/ranking.json` (written with defaults on first run):
`{"weights":{"payout":0.35,"findability":0.30,"competition":0.25,"friction":0.10},"fx":{"EUR":1,"USD":0.92,"GBP":1.17}}`

Feedback (BE-10): `/v1/payouts` and `time.jsonl` give earnings and hours per program, to compare with
the rank and tune the weights on evidence.

## 5. Flow log: `flows/<date>.jsonl`

One line per request the guard saw, allowed or refused:

```json
{"ts":"2026-11-13T12:00:00.123Z","program":"intigriti-acme-acme","actor":"human|claude|tool:<name>",
 "category":"target|recon-source","decision":"allowed|refused","reason":null,
 "method":"GET","url":"https://app.acme.com/api/me","host":"app.acme.com","ip":"203.0.113.7",
 "status":200,"req_bytes":512,"resp_bytes":2048,"ms":143,"egress_ip":"198.51.100.20","flow_id":"<mitm id>"}
```

`actor` comes from the `X-Bounty-Actor` request header, which `bounty-run` sets and the guard
**strips** before forwarding. Requests without it are `human` (Burp, the terminal). `egress_ip` is
looked up once per guard start via a `recon-source` and logged on every line.

## 6. CLI: `host/bounty`

| Command | Does | Exit codes |
|---|---|---|
| `programs sync [--handle H]` | list + detail for every open program (≤ 1 req/s) + new activities, write raw + `program.json`; 403 on detail = `needs-join`, not an error | 0 ok, 2 auth, 3 API/network, 4 parse |
| `programs list [--filter …]` | table of synced programs | 0 |
| `programs rank [...]` | §4 | 0 |
| `programs changes [--since TS]` | diff of the last two syncs, appended to each `history.jsonl` | 0, 1 if nothing changed |
| `program use H` | write `active`, `docker exec … bounty-ctl reload` | 0, 5 if the container is not running |
| `program show [H]` | profile, scope, rules, legal basis | 0 |
| `guard start` / `guard stop [--purge]` | start `kali-bounty` (stops `kali-lab`) / remove it (`--purge` also the guard volume) | 0, 5 Docker not running |
| `guard status [--json]` | `guard.json` via docker exec | 0, 5 |
| `flows [--handle H] [--tail N] [--json]` | the flow log via docker exec | 0, 5 |
| `flows export <handle> <dir>` | copy that program's flows (jsonl + mitm) to a Mac folder for report evidence | 0, 5 |
| `time start|stop [note]` | append to `time.jsonl` | 0 |

Errors go to stderr as one line `bounty: <code>: <message>`, never containing a token. Usage errors
(bad flags, unknown `--handle`) exit 64. A failed detail request is recorded in the sync index; the
sync still exits 0.
`history.jsonl` line: `{"ts","kind":"program-new|scope-added|scope-removed|bounty-changed|rules-changed","detail":{…,"from_sync","to_sync"}}`.
Scope entries are compared by (type, value, tier), in and out, only when both snapshots have scope.

Inside the container: `bounty-run --actor <human|claude> -- <cmd…>` runs a command as uid `hunter` in `/bounty/work/<active handle>`
with the proxy env and `X-Bounty-Actor` set (curl/httpx/katana/ffuf/nuclei get it through their
header flags or `HTTP_PROXY` with a per-actor guard port if a tool can't add headers; Backender
decides, documents it in the PR). With `--actor claude` and `automation: forbidden`, active tools
(the list in `container/share/bounty/active-tools`) are refused with exit 6.

## 7. Finding: `findings/<id>/finding.md` (draft)

`<id>` = `F<YYYYMMDD>-<n>`. YAML front matter: `title, severity, cvss, asset, status (draft|submitted|accepted|duplicate|informative|na), flows: [flow_id…], ai_used: bool`. Body sections: Summary, Steps to reproduce, Impact, Evidence, AI usage.

## 8. Secrets (ADR 006)

| File | Keys | Read by |
|---|---|---|
| `workspace/secrets/intigriti.enc.yaml` | `INTIGRITI_TOKEN`, `INTIGRITI_USERNAME` | `host/bounty` via `sops exec-env` |
| `secrets/*.enc.yaml` (repo) | project secrets (CI, signing); none yet | CI, release |

`.sops.yaml` rules cover both paths. Nothing decrypted is written to disk. The required request
header value comes from `INTIGRITI_USERNAME` and is passed to the guard at `program use` time via
`docker exec -e`, kept in the guard's memory only.

## 9. App ↔ backend: Programs screen (Tauri commands)

The app never talks to Intigriti itself. Rust runs the same CLI the terminal uses and passes its JSON
through. Repo root = `CARGO_MANIFEST_DIR/../..` (as for `tutor/`). When spawning, Rust prepends
`/opt/homebrew/bin:/usr/local/bin` to `PATH` and sets `SOPS_AGE_KEY_FILE` to
`~/.config/sops/age/keys.txt` if it is unset (a GUI app doesn't get the shell's environment).

| Command | Runs | Returns |
|---|---|---|
| `bounty_ranking()` | `host/bounty programs status --json` and `host/bounty programs rank --json --include-join` | `Ranking` |
| `bounty_refresh(on_event: Channel<SyncEvent>)` | `scripts/secrets.sh exec intigriti -- host/bounty programs sync`, then `host/bounty programs changes --json` | `()` after `done`/`error` was sent; a second call while one runs returns `Err("refresh already running")` |
| `open_program(url)` | `/usr/bin/open <url>` | `()`; `Err` unless `url` starts with `https://app.intigriti.com/` |

```ts
type Ranking = {
  status: { last_sync_at: string | null; programs: number; open: number; needs_join: number };
  programs: RankedProgram[];            // rank --json order: ranked first, then join (unless mixed), then excluded
};
type RankedProgram = {
  handle: string; name: string; url: string;
  total: number | null;                 // null when excluded
  factors: { payout: number | null; findability: number | null; competition: number | null; friction: number | null };
  inputs: { max_eur: number | null; min_eur: number | null; surface: number | null;
            days_since_scope_change: number | null; confidentiality: string | null;
            rate_limit_rps: number | null; [k: string]: unknown };
  flags: string[];                      // "join", "hdr", "no-auto", …
  excluded: string | null;              // "no-bounty" | "no-safe-harbour" | "no-web-scope" | null
};
type SyncEvent =
  | { kind: "progress"; done: number; total: number; text: string }
  | { kind: "done"; changes: ChangeEvent[] }        // the UI then calls bounty_ranking() again
  | { kind: "error"; code: string; message: string }; // code from `bounty: <code>:`; message has no token
type ChangeEvent = { ts: string; handle: string; kind: string; detail: Record<string, unknown> };
```

CLI additions behind this (BE-12):
- `programs sync` is **incremental** by default: list + new activities, then detail only for programs
  that are new, changed in the list record (status, bounty, confidentiality), have a new domains /
  rules / status activity since the last sync, have no detail yet, or whose detail is > 7 days old.
  `--full` re-fetches every open program's detail. Still ≤ 1 request/second.
- Progress on stderr, one line per detail request: `bounty: progress: <done>/<total> <handle>`.
- `programs status --json` → `Ranking.status`.
- `programs rank --json` items gain `name` and `url`.
- `programs changes --json` → `ChangeEvent[]` (exit codes unchanged: 1 = nothing changed, still valid JSON `[]`).

### 9.1 Guard controls in the app (slice 2b)

| Command | Runs | Returns |
|---|---|---|
| `bounty_guard_status()` | `host/bounty guard status --json` (exit 5 / container missing → `running: false`) | `GuardStatus` |
| `bounty_use(handle)` | `host/bounty guard start` if `kali-bounty` isn't running (it stops `kali-lab`), then `scripts/secrets.sh exec intigriti -- host/bounty program use <handle>` | `GuardStatus` after the switch; `Err(message)` with the CLI's `bounty: <code>: <message>` text |
| `bounty_guard_stop()` | `host/bounty guard stop` (never `--purge`) | `()` |
| `bounty_open_burp()` | `host/bounty burp` | `()`; the CLI's warning line (Burp already open) comes back as `Ok` with a note → return `string | null` |
| `start_lab()` (existing) | now first runs `host/bounty guard stop` if `kali-bounty` is running | unchanged |

`handle` must match `^intigriti-[a-z0-9-]{1,100}$` before anything is spawned.

```ts
type GuardStatus = {
  running: boolean;                     // kali-bounty container running
  status: "active" | "no-profile" | "error" | "down" | "starting" | null;
  program: string | null;               // active handle
  rate_rps: number | null; automation: string | null;
  egress_ip: string | null;
  last_error: string | null;
  last_refusals: { ts: string; url: string; reason: string }[];   // newest first, max 5
};
```

UI (FE-4): `u` on the selected row = use that program (confirm only when switching away from a
different active program); the header shows the guard: `Guard · Dropbox · 5 req/s · active`
(or `Guard off`), polling `bounty_guard_status()` every 5 s while Bug bounty mode is visible; the
active program's row is marked; `b` opens Burp through the guard; `g` stops the guard. Lab mode's
start button keeps working and the bounty header then shows `Guard off`.

### 9.2 Background sync and alerts (BE-17, FE-5)

While the app runs, Rust repeats `bounty_refresh` every **20 min** (±2 min jitter; skipped if a
refresh is already running; first run 20 min after the start-up refresh). Each finished refresh
emits the app event `bounty-synced` with the same `ChangeEvent[]` as `done`, so the Programs screen
reloads the ranking without a key press.

New `ChangeEvent.kind`s from `programs changes` (BE-17, Python):
- `access-granted`: a program's detail went from `needs-join` to `ok` (you were accepted).
- `access-lost`: from `ok` to `needs-join`.

**Notifications** (macOS, via `/usr/bin/osascript -e 'display notification …'`, no plugin; title
"Lab Tool", text ≤ 120 chars, program names escaped for AppleScript) for, per sync, at most 3
(the rest summarised as "+N more changes"):

| Event | When | Text |
|---|---|---|
| `access-granted` | always | `Access granted: <name> — scope is in the app` |
| `program-new` | `max_eur > 0` | `New program: <name> · max <€> · <pub/reg/app>` |
| `scope-added` | program is the guard's active one, `following` on Intigriti, or in the ranked top 25 | `<name>: new scope <value> (<tier>)` |
| `bounty-changed` | max went up | `<name>: bounty up to <€>` |
| `rules-changed` | program is the guard's active one | `<name>: rules changed — re-read before testing` |

No notification when the app is not running (a launchd job is a later task). Notifications never
contain tokens or anything from the guard's flows.

`bounty_alert_state()` → `{ next_sync_at: string | null, last_sync_at: string | null, last_alerts: {ts: string; text: string}[] }`
for the status line ("Next sync 14:20").
