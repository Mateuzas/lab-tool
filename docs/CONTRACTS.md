# Contracts

Only the Architect edits this file. If code and contract disagree, report it; don't "fix" either side
alone. Sections marked **draft** are expected to change once the Intigriti API has been explored (BE-1).

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
    programs/<handle>/
      raw/<UTC timestamp>/*.json   API responses exactly as received
      program.json                 normalised profile (§2)
      history.jsonl                one line per change found by `programs changes` (§6)
      notes.md                     free text, the user's
      recon/                       tool output, one file per run: <tool>-<UTC timestamp>.<ext>
      flows/<UTC date>.jsonl       guard flow log (§5)
      flows/<UTC date>.mitm        full mitmproxy flows for evidence
      findings/<id>/finding.md     a finding (§7), with evidence files beside it
      time.jsonl                   hours spent: {"start","end","note"} (feeds ranking feedback)
```

`<handle>` is the platform handle, lower case, `[a-z0-9-]`, prefixed with the platform:
`intigriti-<company>-<program>`. Timestamps: UTC, `YYYYMMDDTHHMMSSZ` in names, ISO 8601 in JSON.

## 2. Program profile: `program.json` (draft until BE-1)

```json
{
  "schema": 1,
  "handle": "intigriti-acme-acme",
  "platform": "intigriti",
  "platform_id": "<id from the API>",
  "name": "Acme",
  "url": "https://app.intigriti.com/programs/acme/acme/detail",
  "status": "open",                       // open | suspended | closed
  "confidentiality": "public",            // public | registered | application | private
  "type": "bounty",                       // bounty | vdp
  "industry": "Retail",
  "launched_at": "2025-03-01T00:00:00Z",  // null if unknown
  "synced_at": "2026-11-13T12:00:00Z",
  "currency": "EUR",
  "bounty": {
    "min": 50, "max": 10000,
    "by_severity": {"low": 100, "medium": 500, "high": 2000, "critical": 10000}  // null values allowed
  },
  "scope": {
    "in":  [{"type": "wildcard", "value": "*.acme.com", "tier": 1, "description": ""}],
    "out": [{"type": "url", "value": "https://status.acme.com", "description": ""}]
  },
  "rules": {
    "rate_limit_rps": 5,                  // null = not stated; the guard then uses 5
    "request_header": {"name": "X-Intigriti-Username", "value": "<filled from secrets at runtime>"},
    "automation": "allowed",              // allowed | limited | forbidden | unknown
    "ai_use": "allowed-disclosed",        // allowed-disclosed | forbidden | unknown
    "test_accounts": "self-register",     // self-register | provided | none | unknown
    "text": "<rules of engagement, as written>"
  },
  "legal_basis": "program",               // program | lt-article-25   (ADR 008)
  "stats": {                              // whatever the API exposes; null when absent
    "submissions": null, "accepted": null, "researchers": null,
    "avg_first_response_days": null, "avg_bounty_days": null
  },
  "signals": {                            // computed by `programs rank`, never hand-edited
    "scope_wildcards": 1, "scope_assets": 1, "last_scope_change_at": null
  }
}
```

Scope entry `type`: `wildcard` (`*.example.com`, matches subdomains at any depth, **not** the apex),
`domain` (exact host), `url` (scheme + host + path prefix), `ip`, `cidr`, `mobile`, `other`.
Only `wildcard`, `domain`, `url`, `ip` and `cidr` are enforceable by the guard; the rest are shown
but never matched.

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
6. rate limit bucket empty → wait up to 10 s, then `rate-limited`
7. otherwise allow, add the required header, forward.

`url` entries also check the path prefix (after the request line is known).

### 3.2 Refusal response
`HTTP/1.1 403 Forbidden`, header `X-Bounty-Guard: refused; reason=<code>`, body one line of text.
Reason codes: `no-profile`, `host-mismatch`, `out-of-scope`, `not-in-scope`, `internal-address`,
`rate-limited`, `automation-forbidden`.

### 3.3 Ports and processes
| What | Where |
|---|---|
| guard listen | `127.0.0.1:8080` in `kali-bounty`, published `127.0.0.1:8081` on the Mac |
| guard user | uid `guard` (only uid allowed new outbound connections) |
| tool user (human) | uid `hunter`; env `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` = `http://127.0.0.1:8080` |
| guard CA | generated per container at first start in `/bounty/.guard-ca/` (tmpfs copy for mitmdump); installed in the container's trust store; Burp is told not to validate the upstream |
| control | `docker exec kali-bounty bounty-ctl reload` re-reads `/bounty/active` and the profile |

### 3.4 `recon-sources.json`
`{"hosts": ["crt.sh", "web.archive.org", ...], "rate_limit_rps": 2}`. Exact hosts only, no wildcards.

## 4. Ranking (draft until BE-1; ADR 007)

`host/bounty programs rank [--json] [--top N] [--explain <handle>]`

Each program gets factor scores in `0..1` and one total:

```
total = w_pay·payout + w_find·findability + w_comp·(1 − competition) − w_fric·friction
```

| Factor | Inputs (use what exists; missing inputs are skipped and renormalised, never guessed) |
|---|---|
| `payout` | medium and high bounty amounts (weighted more than `max`, which is rarely paid), log-scaled across all programs |
| `findability` | number of wildcards and assets, asset types (web/API > static), self-register test accounts, recent scope additions, `type` = bounty |
| `competition` | program age, confidentiality (private < application < registered < public), submission/researcher counts if exposed, brand size bucket (manual tag in config) |
| `friction` | automation forbidden, slow first response / bounty, strict rules, KYC-limited |

Hard filters (excluded, listed separately with the reason): `status` ≠ `open`, `type` = `vdp` unless
`--include-vdp`, no enforceable in-scope entry, `ai_use` = `forbidden` when `--copilot` is set.

`--json` output: `[{"handle","total","factors":{"payout","findability","competition","friction"},"inputs":{...},"excluded":null|"<reason>"}]`
sorted by `total` descending. Text output: one line per program, factor columns visible.

`config/ranking.json`: `{"weights":{"payout":0.35,"findability":0.30,"competition":0.25,"friction":0.10},"brand_size":{"<handle>":"large"}}`.

Feedback loop (later, BE-8): `time.jsonl` and findings give hours and outcomes per program; a report
compares rank with your own results so the weights can be tuned on evidence.

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
| `programs sync [--handle H]` | fetch from Intigriti, write raw + `program.json` | 0 ok, 2 auth, 3 API/network, 4 parse |
| `programs list [--filter …]` | table of synced programs | 0 |
| `programs rank [...]` | §4 | 0 |
| `programs changes [--since TS]` | diff of the last two syncs, appended to each `history.jsonl` | 0, 1 if nothing changed |
| `program use H` | write `active`, `docker exec … bounty-ctl reload` | 0, 5 if the container is not running |
| `program show [H]` | profile, scope, rules, legal basis | 0 |
| `guard status` | active program, rate limit, last refusals | 0 |
| `time start|stop [note]` | append to `time.jsonl` | 0 |

Errors go to stderr as one line `bounty: <code>: <message>`, never containing a token.
`history.jsonl` line: `{"ts","kind":"program-new|scope-added|scope-removed|bounty-changed|rules-changed","detail":{...}}`.

Inside the container: `bounty-run --actor <human|claude> -- <cmd…>` runs a command as uid `hunter`
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
