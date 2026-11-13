# Tasks

Status: `todo` · `in progress` · `blocked` · `done`. Only the Architect edits this file.
Every task ends with a PR into `main` and the checks in AGENTS.md. Work goes **one slice at a time**;
each slice ends with a real hunting session and a short friction list from the user, which shapes the
next slice (ADR 003).

## Done

| What | PRs |
|---|---|
| Lab mode: spike gates, container, supervisor, kill switch, Tauri app, tutor, CI | history before PR #1 |
| Lab: tutor live view, end sessions to machines that leave scope | #1 |
| Architect docs for Bug bounty (this file, ARCHITECTURE, CONTRACTS, ADR 001–009) | #2 |
| SOPS/age secrets, secret scan in CI (OPS-1); age key rotated after exposure | #3, #4 |
| Repo moved off the iCloud Desktop to `~/Developer/kali-lab`: `make lint` and gitleaks work in place (OPS-2) | – |
| Intigriti token stored (USER-1); API explored, field report `docs/research/intigriti-api.md` (BE-1); CONTRACTS §2–§4 final (ARC-2) | this PR |

## Slice 0: secrets and API access

| Id | Owner | Task | Depends | Status | Done when |
|---|---|---|---|---|---|
| OPS-1 | DevOps | SOPS/age setup (ADR 006): dedicated kali-lab age key appended to `~/.config/sops/age/keys.txt`, `.sops.yaml` (rules for `secrets/` and `workspace/secrets/`), `scripts/secrets.sh` (edit, exec, rekey, check), `scripts/check-secrets.sh`, gitleaks workflow, `.gitignore` for `*.dec.*` and keys, `docs`-ready text for SECRETS (report it; Architect writes `docs/SECRETS.md`) | – | done | `scripts/secrets.sh edit intigriti` creates `workspace/secrets/intigriti.enc.yaml`; `scripts/secrets.sh check` and gitleaks pass in CI; no plaintext anywhere |
| USER-1 | User | Log in to Intigriti, create a personal access token, store it with `scripts/secrets.sh edit intigriti` (`INTIGRITI_TOKEN`, `INTIGRITI_USERNAME`). Never paste it in chat | OPS-1 | done | file exists and decrypts |
| OPS-2 | DevOps | `make lint` works on this Mac (iCloud Desktop + Docker file sharing gives `resource busy`): lint a temporary copy, or move the clone off the Desktop | – | done | `make lint` passes in place |
| BE-1 | Backender | **Intigriti API discovery**: `bounty/intigriti.py` minimal client (token from env), fetch the program list and 3 programs' details, domains and rules into `workspace/bounty/programs/<h>/raw/`; write a field report (which fields exist for each CONTRACTS §2/§4 input, real paths, pagination, rate limits). No normaliser yet | USER-1 | done | raw files on disk; report back; Architect updates CONTRACTS §2, §4 |

## Slice 1: program intake and ranking (Mac only, no target traffic)

| Id | Owner | Task | Depends | Status | Done when |
|---|---|---|---|---|---|
| BE-2 | Backender | `host/bounty programs sync` + normaliser to `program.json` (CONTRACTS §2, §6), activities, sync index, exit codes | BE-1, ARC-2 | done (#6) | unit tests on recorded raw responses (fixtures scrubbed of tokens); a real sync of all visible programs |
| BE-3 | Backender | `programs rank` with factor scores, hard filters, `--explain`, `--json`, default `config/ranking.json` (CONTRACTS §4) | BE-2 | done (#6) | unit tests per factor and for missing inputs; top 20 printed for review |
| BE-4 | Backender | `programs changes` + `history.jsonl` (new programs, scope added/removed, bounty and rules changed) | BE-2 | done (#6) | tests with two synthetic syncs |
| ARC-2 | Architect | Finalise CONTRACTS §2/§4 from the BE-1 report | BE-1 | done | – |
| ARC-3 | Architect + user | Review the ranking together, adjust weights, **pick the first program**, read its full rules | BE-3 | in progress | `program use` target chosen; rules read by the user |

## Slice 2: scope guard (before any request to a target)

| Id | Owner | Task | Depends | Status | Done when |
|---|---|---|---|---|---|
| OPS-4 | DevOps | Image: mitmproxy (pinned), users `guard` and `hunter`, recon tools (subfinder, httpx, katana, ffuf, nuclei; pinned); `host/bounty` container start/stop for `kali-bounty` per ADR 002 | – | todo | image builds in CI; `kali-bounty` starts with only `workspace/bounty` mounted |
| BE-5 | Backender | `bounty-guard` addon + `bounty-supervisor` + iptables uid lock + `bounty-ctl reload` + `bounty-run` (CONTRACTS §3, §5, §6) | OPS-4, BE-2 | todo | `tests/bounty-guard-test.sh` proves, with local listeners and a fake profile: no profile refuses all; out-of-scope beats in-scope; host mismatch refused; internal addresses refused; a tool ignoring the proxy can't connect; rate limit holds; header added; actor header stripped; every flow logged |
| BE-6 | Backender | `program use` wires the active profile and the header value; Burp launch with upstream proxy `127.0.0.1:8081` | BE-5 | todo | Burp request to an in-scope test host shows in the flow log; out-of-scope gets the 403 |
| BE-7 | Backender | `hunter/` Claude Code project: CLAUDE.md draft for the Architect, settings (read the program folder, Burp MCP read-only, `bounty-run --actor claude` only, approval per command) | BE-5 | todo | Claude can read a profile and run an approved in-scope `curl`, labelled `claude` in the log; an unapproved command doesn't run |
| USER-2 | User | **First hunting session** on the chosen program with the CLI + Burp; keep a friction list | BE-6 | todo | friction list → next slice |

## Later (in rough order, re-planned after each hunting session)

| Id | Owner | Task |
|---|---|---|
| BE-8 | Backender | Recon pipeline per program (passive first), diff of new hosts between runs |
| BE-9 | Backender | Findings: `finding new` from flow ids, report draft with AI-usage section, export for Intigriti |
| BE-10 | Backender | Ranking feedback: hours and outcomes vs. rank report |
| FE-1 | Frontender | Bug bounty screens, keyboard-first: Programs (ranked table), Program, Flows, Findings; `docs/design.md` Bug bounty section first (Architect) |
| OPS-3 | DevOps | `.ovpn` files in SOPS; Lab container decrypts into tmpfs at connect |
| BE-11 | Backender | SOCKS path with IP scope for non-HTTP tools where a program allows it |
| – | – | Other platforms; Lithuanian programs (ADR 008 `lt-article-25`); Analyze mode |
