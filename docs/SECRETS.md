# Secrets

Every secret is encrypted with [SOPS](https://github.com/getsops/sops) and
[age](https://age-encryption.org) (ADR 006). Plaintext exists only in a running process's
environment, never in a file, a log, a PR or chat.

## Where secrets live

| Path | What | In git? |
|---|---|---|
| `workspace/secrets/*.enc.yaml` | **your** secrets: API tokens (Intigriti), later VPN files | no (`workspace/` is user data) |
| `secrets/*.enc.yaml` | project secrets (CI, signing); none yet | yes, encrypted |

| File | Keys | Used by |
|---|---|---|
| `workspace/secrets/intigriti.enc.yaml` | `INTIGRITI_TOKEN`, `INTIGRITI_USERNAME` | `host/bounty` (CONTRACTS §8) |

`.sops.yaml` encrypts both paths to the kali-lab age **public** key. The private key is
`~/.config/sops/age/kali-lab.key`, also appended to `~/.config/sops/age/keys.txt`, which sops reads
through `SOPS_AGE_KEY_FILE` (set in `~/.zshrc`).

**Back up `kali-lab.key` in your password manager.** Without it the encrypted files can't be read.
Tokens can be re-issued, but that's the only way back.

## Everyday use

```sh
scripts/secrets.sh edit intigriti        # opens it decrypted in $EDITOR, re-encrypts on save
scripts/secrets.sh edit --project <name> # the same for a committed project secret in secrets/
scripts/secrets.sh list                  # names and keys, never values
scripts/secrets.sh exec intigriti -- host/bounty programs sync   # values only in that process's env
make secrets-check                       # what CI runs (also: scripts/secrets.sh check)
```

`scripts/secrets.sh` works on the checkout it is run from (`git rev-parse --show-toplevel`), and
`workspace/` exists only in the main clone (`~/Developer/kali-lab`), so run `edit`/`exec` there,
not in a worktree. The app runs `scripts/secrets.sh exec intigriti -- …` itself for sync and
`program use`, and sets `SOPS_AGE_KEY_FILE` to `~/.config/sops/age/keys.txt` when it is unset
(a GUI app doesn't get `~/.zshrc`).

Secrets in the Bug bounty flow:
- `INTIGRITI_USERNAME` reaches the scope guard only through `docker exec -e` at `program use`
  and is kept in the container's tmpfs (`/dev/shm`), never on disk; a container restart drops it.
- Access-control check sessions (`host/bounty ac session`) are read from stdin, never argv, and
  stored only in the container's tmpfs (root, 0600). `ac status` and results never show values.
- The guard's flow log and `.mitm` files can contain session cookies and tokens from your test
  accounts. Treat exported flows as sensitive; redact before putting excerpts in a report.
- The Claude copilot (`hunter/`) is denied reads of `workspace/secrets/`, `workspace/vpn/` and
  `~/.config/sops/`.

A new file starts from `scripts/secret-templates/<name>.yaml` (placeholders only) when a template
exists. Edit with an editor that doesn't write backup or swap files next to the file, or set
`EDITOR` to one that keeps them elsewhere (sops edits a temporary copy outside the repo anyway).

## Rules

- Never write decrypted output to disk. `sops exec-env` (through `scripts/secrets.sh exec`) or
  `docker exec -e` for containers; if a file is unavoidable, use tmpfs inside the container.
- Never paste a secret into chat, PR text, commit messages, issues, test fixtures or logs.
- Code that handles a token must not print it, including in error messages (CONTRACTS §6).

## What the checks catch

`scripts/check-secrets.sh` (`make secrets-check`, and the `Secrets` workflow on every PR):

1. every tracked file in `secrets/` is SOPS-encrypted (all values `ENC[...]`, has a MAC);
2. no tracked `.env`, `.ovpn`, key files, `*.dec.*` or anything under `workspace/`;
3. no key material or known token formats in tracked text (age keys, PEM private keys, inline
   OpenVPN keys, GitHub/AWS/Anthropic/Slack/Google tokens, a plaintext `INTIGRITI_TOKEN`);
4. gitleaks over the whole history (default rules + `.github/gitleaks.toml`). It scans a temporary
   clone, because in an iCloud-synced folder it hangs on the working tree (the repo used to live on
   the Desktop; it is now in `~/Developer/kali-lab`).

## Adding a person or CI

1. They run `age-keygen -o kali-lab.key` and send you the **public** key (`age1…`).
2. Append it to the `age:` list in `.sops.yaml` (comma-separated).
3. `scripts/secrets.sh rekey`, then commit.

Removing someone: delete their key, rekey, **and rotate the secrets**. Old ciphertext stays in git
history and they may have kept plaintext.

## If a secret leaks

1. Revoke or rotate it with the provider first (for Intigriti: delete the token in account settings).
2. Store the new value with `scripts/secrets.sh edit …`.
3. Treat anything that was pushed as burned. Rewriting history doesn't un-publish it.
