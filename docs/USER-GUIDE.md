# Bug bounty mode: user guide

For daily use of the app and `host/bounty`. Formats and exit codes are in
[CONTRACTS.md](CONTRACTS.md); how it works inside is in [ARCHITECTURE.md](ARCHITECTURE.md).
Examples use `acme.test` / `example.com` placeholders.

Run CLI commands from the repo root (`~/Developer/kali-lab`). Commands that need your Intigriti
token or username go through `scripts/secrets.sh exec intigriti -- …`; the app does this for you.

## The one rule the tool enforces

Nothing in `kali-bounty` can reach the internet except through the **scope guard**. The guard
enforces exactly one program at a time (the **active** program), refuses everything out of scope
or over the rate limit, adds the program's required header, and logs every request with who sent
it (`human`, `claude`, `tool:<name>`). With no active program, everything is refused. Burp, the
shell, the Claude copilot, recon mapping and the access-control check all go through it.

## Daily workflow

1. **Open Lab Tool.** Bug bounty is the default mode. The Programs table shows the last ranking at
   once and refreshes from Intigriti in the background.
2. **Read what changed.** `c` opens the changes since the last sync and recent alerts (new programs,
   new scope, bounty raises, access granted, rules changed, program closed).
3. **Pick a program.** Move with `j`/`k`, read it on Intigriti with `Enter`, then press **`u`** to
   load it into the guard. Switching away from another active program asks for a second `u`.
   The header shows `Guard · <program> · <rate> req/s · active`.
4. **Work.**
   - **`b`** Burp, with the guard as upstream proxy. Browse as test account A.
   - **`t`** a Terminal.app shell inside `kali-bounty` (`[<handle>] /bounty/work/<handle> $`).
   - **`h`** the Claude copilot in Terminal.app; you approve each command it wants to run.
5. **Find where to look.** **`l`** opens the look-here list (hosts from passive recon, most
   interesting first). Press **`m`** there (or on the table) to map the active program: httpx +
   katana through the guard. The list then fills with status, titles, tech and URL counts.
6. **Check access control** between your two test accounts with `host/bounty ac …` (below).
7. **Draft findings** in `workspace/bounty/findings/<handle>-<slug>/finding.md` (the copilot can
   write these; CONTRACTS §7 for the fields). Pull evidence with `host/bounty flows export`.
   Confirm everything by hand and submit on Intigriti yourself.
8. **Stop.** `g` stops the guard (removes the container; flows and hunter's home are kept).
   Starting Lab mode also stops it.

## What runs on its own, and what only on a key press

| Automatic | Only when you ask |
|---|---|
| Programs sync at app start, then every 20 min ± 2 (only while the app runs) | `r` refresh now |
| macOS notifications, at most 3 per sync (see below) | `u` load a program into the guard |
| Passive recon (crt.sh + DNS from the Mac, no traffic to the program): after a sync for new in-scope wildcards on the active, followed or top-25 programs (≤ 3 per sync), and the first time you `u` a program | `m` / `recon map`: active mapping, and `e` / `recon exposures`: curated exposure checks; active program only, through the guard |
| Guard reload when a sync changes the active program's scope, rules or rate | `ac replay`: only the requests you name |
| Guard status poll every 5 s while the screen is visible | `b`, `t`, `h`: Burp, shell, copilot |

Nothing sends a request to a program's own hosts unless you pressed a key or typed a command.

Notifications: access granted (always); new program with a bounty; new scope on the active,
followed or top-25 programs; bounty raised; rules changed or program suspended/closed on the active
(or a followed) program. They never contain tokens or flow data.

## Keys

### Programs screen

| Key | Does |
|---|---|
| `j` `k` / `↓` `↑` | move the selection |
| `Home` `End` `G`, `PgUp` `PgDn` | first / last row, a screen at a time |
| `Enter` / `o` | open the program on Intigriti |
| `/` | filter by name, handle or industry (`Esc` clears) |
| click a heading | sort by that column |
| `r` | refresh from Intigriti now |
| `c` | changes since the last sync, and recent alerts |
| `x` | show / hide excluded programs (no bounty, no safe harbour, no web scope) |
| `J` | programs you need to join: listed last, or mixed into the ranking |
| `a` | on a join program: application state none → applied → accepted → rejected → none |
| `u` | load the selected program or manual target into the guard (starts the guard if needed) |
| `b` | Burp through the guard |
| `t` | shell in `kali-bounty` (needs the guard on and a program active) |
| `h` | Claude copilot (same) |
| `g` | stop the guard |
| `R` | requests the guard refused recently (newest 5) |
| `l` | look-here list for the selected program |
| `m` | map the active program (the selected row must be the active one) |
| `?` | shortcut overlay |
| `Esc` | close the overlay / popover / changes, cancel a pending switch, clear the filter |

### Look-here panel

| Key | Does |
|---|---|
| `j` `k`, `Home` `End` `G` | move |
| `Enter` / `o` | show the host's URL (the app never opens it itself: paste it into Burp's browser, which goes through the guard) |
| `y` | copy that URL |
| `m` | map the active program, with progress; one map at a time |
| `e` | check exposures on the hosts a map found live (kali-lab's 13 curated checks: `.git`, `.env`, SQL dumps, actuator, API definitions…), same rules as `m`; a hit shows a filled **exposed** chip (hover it for the checks) |
| `x` | show / hide out-of-scope and not-in-scope hosts |
| `l` / `Esc` | close |

New hosts are highlighted. `m` and `e` (one run at a time between them) explain why they can't run when the guard is off or another program
is active.

## CLI reference

`host/bounty <group> --help` shows every option.

### Programs

| Command | Does |
|---|---|
| `scripts/secrets.sh exec intigriti -- host/bounty programs sync [--handle H] [--full]` | incremental sync (≤ 1 req/s to Intigriti); `--full` re-fetches every open program's detail |
| `programs status [--json]` | last sync time and counts |
| `programs list [--filter TEXT]` | synced programs as a table |
| `programs rank [--top N] [--explain H] [--include-join] [--include-no-bounty] [--json]` | the ranking; `--explain` shows every factor and input for one program |
| `programs changes [--since TS] [--json]` | diff of the last two syncs (exit 1 when nothing changed) |
| `program apply-state H applied\|accepted\|rejected\|withdrawn\|none [--note TEXT]` | record a private-program application |

### Guard, Burp, shell, flows

| Command | Does |
|---|---|
| `guard start` | start `kali-bounty` (stops `kali-lab`); recreates it if its hardening spec is older; waits until the guard is up |
| `scripts/secrets.sh exec intigriti -- host/bounty program use H` | make H the active program (validates the profile first; exit 5 if the guard isn't running) |
| `guard status [--json]` | active program, rate, automation, egress IP, last refusals |
| `guard stop` | remove the container; flows, CA and hunter's home stay in their volumes |
| `guard stop --purge --yes` | also delete both volumes (every flow log and the CA): export evidence first |
| `burp` | launch Burp with upstream proxy `127.0.0.1:8081`; warns if Burp is already open |
| `shell [--handle H]` | login shell as `hunter` in `/bounty/work/<active>`; `--handle` changes only the work dir, never the policy |
| `flows [--handle H] [--tail N] [--json]` | the flow log (default: active program, last 50) |
| `flows export H DIR` | copy H's flows (`.jsonl` + `.mitm`) to a Mac folder for report evidence |

Inside the shell, tools pick up the proxy and actor header through `bounty-run`; `curl`, httpx,
katana, ffuf and nuclei get the flags added for them. A tool that ignores the proxy simply can't
connect.

### Recon

| Command | Does |
|---|---|
| `recon passive H [--domain D]` | crt.sh + one DNS lookup per name, from the Mac; nothing to the program. Exit 3 = crt.sh failed, partial results kept |
| `recon map [--limit N]` | httpx-toolkit + katana on the active program's in-scope hosts, through the guard, at the program's rate. Exit 3 guard not running, 5 no active program, 6 the program forbids automation |
| `recon exposures [--limit N]` | the 13 curated exposure checks on the active program's live hosts (map first), through the guard as `tool:nuclei`; no response bodies kept. Same exit codes as `recon map` |
| `recon list H [--json]` | the look-here list |

Files: `workspace/bounty/work/<h>/recon/` (`hosts.jsonl`, `live.jsonl`, `urls.jsonl`,
`mapped.json`, `state.json`).

### Access-control check (two test accounts)

On demand only; works for the active program. Sessions live in the container's memory (tmpfs)
and are gone after a restart.

```
# 1. Paste each account's session headers (from Burp) on stdin; never as arguments.
pbpaste | host/bounty ac session a --marker alice@example.com --marker 100234
pbpaste | host/bounty ac session b
host/bounty ac status                 # header names and marker counts only, never values

# 2. Browse as A through the guard (Burp). Then list what can be replayed:
host/bounty ac candidates --since 4 --limit 50

# 3. Replay the ones you choose, as B and with no session:
host/bounty ac replay 3f2a… 9b1c…
host/bounty ac replay 77de… --unsafe 77de…   # a POST/PUT/DELETE only when you name it twice

# 4. Read the results, likely problems first:
host/bounty ac results
```

| Command | Does |
|---|---|
| `ac session a\|b [--marker TEXT]... [--clear]` | set or forget an account's session; markers are text only A's data contains |
| `ac status [--json]` | which sessions are set |
| `ac candidates [--since HOURS] [--limit N] [--json]` | A's 2xx in-scope requests (static files skipped); `safe` = GET/HEAD/OPTIONS |
| `ac replay ID... [--unsafe ID]...` | replay through the guard as `tool:ac-check`; non-safe methods refused unless `--unsafe` |
| `ac results [--handle H] [--json]` | per request A / B / none: status, verdict, similarity |

Verdicts per variant: `blocked`, `leak` (a marker appeared), `same-as-a` (≥ 0.95 similar),
`a-data` (A's ids or emails appear), `differs`, `refused`, `error`. A row marked **CHECK**
(`likely-broken-access`) means B got A's data and no-session didn't; `public` means both did.
Every CHECK is a hint: repeat it as B in Burp and compare the data before you report anything.
Results (`work/<h>/ac/results.jsonl`) contain no bodies or header values.

### Manual targets

For a system whose **owner authorised you in writing**, outside any platform (ADR 010):

```
host/bounty manual add "Acme staging" --scope '*.acme.test' --scope acme.test \
  --out status.acme.test --rate 3 --authorized-by "Acme Ltd" \
  --auth-note ~/Documents/acme-authorisation.pdf --expires 2026-12-31
```

This writes `programs/manual-acme-staging/program.json` (`legal_basis: authorization`). Scope
entries: `*.example.com` (subdomains, not the apex), `example.com`, `https://example.com/app`, or
a CIDR. `--rate 0` allows only your own traffic (no tools, no Claude). `--header 'Name: value'`
adds a required header. `--force` overwrites. Manual targets show under the ranked table in a
**Manual targets** section; use `u`/`b`/`g` there as for programs, or
`host/bounty program use manual-acme-staging` from the CLI. They are never synced, ranked or
notified about. The copilot reminds you once if no `--auth-note` is recorded.

## How interest is scored

`recon list` (and the look-here panel) sorts hosts by `interest` (0 to 1), then by when they were
first seen (newest first), then by name. The formula is in `bounty/recon.py`:

```
interest = min(1.0,  0.35 if new  +  0.15 if live  +  0.1 × number of signals  +  0.3 if exposed)
```

**`exposed`**: one or more of the curated exposure checks (`e`, `recon exposures`) matched on the
host. It adds 0.3 once, however many checks hit; the `exposed` chip itself isn't counted as a 0.1
signal too. A hit is a lead: open the URL in Burp's browser and confirm before reporting.

**`new`**: the host's `first_seen` is within the last 7 days **and** later than
`recon/state.json`'s `baseline_at`. The first passive run for a program sets the baseline, so
everything it found is never `new`; only hosts that appear later are.

**`live`**: `true` when a map got an answer (a `live.jsonl` record with a status), `false` when the
host is in `recon/mapped.json` but had no answer (or the guard refused it), `null` when never mapped.
Only `true` scores.

**Signals** are matched against one text made of: the host's **subdomain labels only** (everything
in front of the registrable domain: the last two labels, or the last three for two-level public
suffixes such as `co.uk` or `com.au`; so `acme` in `acme.test` or `acme.co.uk` never counts), the page title and tech from
the map, and the paths of URLs katana found. The text is split into lowercase tokens
(`[a-z0-9]+`); *words* must equal a whole token, *parts* match anywhere in the text.

| Signal | Words (whole tokens) | Parts (anywhere) |
|---|---|---|
| `login` | login, signin, logon, auth, oauth, oauth2, sso, saml, signup, register, account, accounts, password, reset | `sign-in`, `log-in`, and "sign in", "log in", "sign up", "log on" as whole words (not inside "design into") |
| `api` | api, apis, rest, rpc, jsonrpc, v1, v2, v3 | |
| `admin` | admin, administrator, dashboard, manage, management, console, internal, staff, backoffice, cms, panel | |
| `upload` | upload, uploads, import, attachment, attachments, files, media | |
| `graphql` | graphql, gql, graphiql | |
| `swagger` | swagger, openapi, redoc, apidocs | `api-docs`, `swagger-ui` |
| `debug` | debug, trace, actuator, phpinfo, profiler, test, staging, stage, dev, sandbox, qa, uat, preprod | `_profiler` |
| `js-heavy` | 20 or more found URLs ending in `.js`, or a tech name containing react, angular, vue, next.js, nuxt, svelte or ember | |
| `redirect-offsite` | the map's redirect `location` (or final URL) points to a host with a different registrable domain | |

Each signal counts once, however many words match.

**Before a map**, a host has no title, tech, URLs or answer, so only `new` and signals from its own
subdomain labels can score. Mapping is what lifts live, content-rich hosts up the list.

Worked examples:

| Host | State | Signals | Interest |
|---|---|---|---|
| `admin-api.staging.acme.test` | first seen 2 days ago, after the baseline; never mapped | labels `admin-api.staging` → tokens admin, api, staging → `admin`, `api`, `debug` | 0.35 + 0.3 = **0.65** |
| `www.acme.test` | in the baseline; mapped: 200, title "Acme login", tech React + Nginx, URLs `/api/v2/users`, `/upload` | `login` (title), `api` (api, v2), `upload`, `js-heavy` (React); `www` and `acme` match nothing | 0.15 + 0.4 = **0.55** |
| `status.acme.test` | in the baseline; mapped: 301 to `https://acme-status.example.net/` | `redirect-offsite` (example.net ≠ acme.test) | 0.15 + 0.1 = **0.25** |
| `legacy.acme.test` | in the baseline; mapped: 200 "Index of /"; `e` found `.git/config` and `.env` | `exposed` (+0.3, once) | 0.15 + 0.3 = **0.45** |

A new, live host with seven signals would reach 0.35 + 0.15 + 0.7 = 1.2 and is capped at 1.0. The
score orders the list; it doesn't judge a host.

Known limit: the list of two-level suffixes is short (`TWO_LEVEL` in `bounty/recon.py`: co.uk,
com.au, co.jp …); a rarer one still counts its company label as a subdomain word.

## After Docker or the container restarts

The container restarts on its own with Docker Desktop (`--restart unless-stopped`), but its
memory-only state does not survive:

- **The guard comes back with no program**: everything is refused (`no-profile`), and the header
  shows that. Press **`u`** on the program again (or `program use` from the CLI).
- **Access-control sessions are gone**: run `ac session a` / `ac session b` again.
- Flows, the guard CA, hunter's home and everything under `workspace/` are kept.

After `make build` or pulling changes to `container/`: `guard start` (or `u`) recreates
`kali-bounty` if the hardening spec changed. Burp keeps working; the guard CA is in the volume.

## Troubleshooting

**Every request is refused (`X-Bounty-Guard: refused; reason=no-profile`).** No program is active:
after a restart, after `guard start`, or because the last `program use` failed. Press `u` or run
`program use`; check `guard status`.

**`reason=out-of-scope` or `not-in-scope`.** The host is excluded, or not covered by any in-scope
entry. `*.acme.test` does not cover `acme.test` itself. Check `program.json` and the program page;
don't try another host, IP or encoding. If the program's scope just changed, wait for the next sync
or press `r`; the guard reloads by itself.

**`reason=host-mismatch`.** CONNECT target, TLS SNI and `Host` disagree (a tool overriding `Host`,
domain fronting). Send the request to the host you mean.

**`reason=internal-address`.** The name resolves to a private, loopback, link-local, CGNAT or
metadata address, which is refused unless the profile lists it.

**`reason=rate-limited`.** The guard waited up to 10 s for a slot. Slow the tool (`-rl`, `-rate`,
threads) to the program's rate (`guard status`). A program with no stated rate gets 2 req/s.

**`reason=automation-forbidden`.** The program's rate is 0: only your own Burp and shell traffic is
allowed; tools, `recon map`, `recon exposures`, `ac replay` and Claude's active tools are refused.

**The guard refuses after `program use` with a template error.** The program's required header or
User-Agent template has an unknown placeholder, or `INTIGRITI_USERNAME` is missing: run it through
`scripts/secrets.sh exec intigriti --`.

**`t` / `h` are disabled.** Start the guard and load a program first (`u`).

**`m` doesn't map.** Mapping runs only for the active program and one map at a time; the panel says
which. The program may forbid automation (exit 6).

**nmap fails in the shell.** Expected: it doesn't exec in the hardened container (file
capabilities vs an empty bounding set) and raw TCP can't leave anyway. The guard is HTTP(S) only.

**Burp traffic doesn't show in `flows`.** Burp must use the upstream proxy `127.0.0.1:8081`
(Settings > Network > Upstream proxy); `b` launches it that way. If Burp was already open (`b` warns),
quit it and press `b` again.

**Sync fails with an auth error.** The token expired or was revoked: create a new one on
Intigriti and store it with `scripts/secrets.sh edit intigriti`.
