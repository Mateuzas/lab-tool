# Notes for agents and contributors

Read [README.md](README.md), then [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/CONTRACTS.md](docs/CONTRACTS.md) and your tasks in [docs/TASKS.md](docs/TASKS.md). How the user works
with Bug bounty mode: [docs/USER-GUIDE.md](docs/USER-GUIDE.md).
Decisions and their reasons are in [docs/adr/](docs/adr/). Ask before changing scope or architecture.

## Roles and ownership

Work is split by the [Architect Protocol](ARCHITECT.md). kali-lab already has its own layout, so
the protocol's `backend/ frontend/ infra/` folders map onto existing directories (ADR 001).

| Role | Owns | Typical work |
|---|---|---|
| **Architect** (main session) | `docs/`, `AGENTS.md`, `ARCHITECT.md`, `CLAUDE.md`, `README.md`, `tutor/CLAUDE.md`, `hunter/CLAUDE.md` | design, contracts, task briefs, review, merging, integration runs |
| **Backender** | `bounty/`, `container/bin/`, `container/share/`, `host/`, `app/src-tauri/`, `tests/`, `tutor/` and `hunter/` settings | bounty CLI, Intigriti client, ranking, scope guard, supervisor, Rust backend, tests |
| **Frontender** | `app/src/`, `app/index.html`, `app/scripts/` | screens, keyboard handling, tables, the preview mock |
| **DevOps** | `container/Dockerfile`, `.github/`, `Makefile`, `.sops.yaml`, `secrets/`, `scripts/`, `.gitignore`, `app/package*.json`, `app/src-tauri/Cargo.*`, `app/src-tauri/tauri.conf.json` | image, CI, SOPS/age secrets, release builds, dependency pins |

### Rules for every role
- Edit only what you own. If you need a change elsewhere, stop and report it to the Architect.
- `docs/` is the Architect's. If a contract is wrong, report it; don't edit it.
- Each task gets its own branch (`<area>/<short-name>`) and worktree, and ends with a pull request into
  `main` (`gh pr create`). The Architect reviews and merges. Don't merge your own PR.
- Before opening a PR: `make lint`, `make app-test` if `app/` changed, `make test-bounty` if
  `bounty/`, the guard or `container/` changed (unit tests + `tests/bounty-guard-test.sh`, local
  listeners only; CI runs both too), the tests your task names, and `scripts/secrets.sh check`.
- Report back in at most 25 lines: files changed, commands and results, contract deviations, open
  questions, and what you did **not** verify (e.g. "not run against a real program").

### Hard rules (security)
- **No traffic to a bug bounty target except through the scope guard** (ADR 005). Never add a path
  around it, never disable it "for a test". Tests use local listeners or the guard's dry-run mode.
- **Claude never sends traffic to HTB/THM machines** (Lab mode; README "The tutor").
- **Secrets only through SOPS** (ADR 006): no plaintext tokens, `.ovpn` contents or API keys in git,
  logs, PR text, chat or test fixtures. Read them with `sops exec-env`, never write them to disk.
- The container is never `--privileged`; published ports bind to `127.0.0.1` only.
- The clone lives at `~/Developer/kali-lab`; worktrees go beside it (e.g. `~/Developer/kali-lab-<task>`)
  and point `BOUNTY_WORKSPACE` at the main clone's `workspace/bounty` when they need data. Never put the
  clone or a worktree in an iCloud-synced folder such as `~/Desktop`: git, Docker file sharing and
  gitleaks fail there (`mmap failed`, `resource busy`). The installed app uses the clone it was built from.

## Repo map

- `app/`: Tauri 2 desktop app (Rust backend `src-tauri/`, TypeScript UI `src/`).
- `container/`: the Kali image both modes run from (Lab: `kali-lab`, Bug bounty: `kali-bounty`).
- `host/`: Mac-side CLIs: `host/lab` (Lab mode), `host/bounty` (Bug bounty mode).
- `bounty/`: Python package (stdlib only) behind `host/bounty`: Intigriti client, program profiles,
  ranking, manual targets, guard control, recon, access-control check. Tested with
  `python3 -m unittest discover bounty`.
- `tutor/`: sandboxed Claude Code tutor for Lab mode. `hunter/`: Claude Code copilot for Bug bounty.
- `scripts/`: `secrets.sh` (SOPS wrapper), `check-secrets.sh`, secret templates. `tools/`: dev utilities.
- `tests/`: shell gate and fault-injection tests (Lab), `bounty-guard-test.sh` (scope guard end to end).
- `docs/`: architecture, contracts, tasks, ADRs, `USER-GUIDE.md`, `SECRETS.md`, `spike-notes.md`
  (history), `design.md` (UI).
- `workspace/`: user data, gitignored (layout in CONTRACTS §1; volumes and tmpfs in ARCHITECTURE §3).

## Commit conventions

- One logical change per commit; keep the tree building.
- Subject: `<type>: <imperative summary>` under ~72 chars, as in the existing history.
  Types: `feat`, `fix`, `docs`, `test`, `ci`, `chore`, `refactor`.
- Body: why, anything non-obvious, and the task id (e.g. `BE-3`).
