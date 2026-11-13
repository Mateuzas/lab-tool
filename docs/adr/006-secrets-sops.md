# ADR 006: All secrets are SOPS/age encrypted

**Status:** accepted. **Date:** 2026-11-13.

**Context.** The user wants every secret under SOPS, as in Wander. Bug bounty adds a personal
Intigriti API token; Lab already holds `.ovpn` files with credentials in plaintext in
`workspace/vpn/`.

**Decision.** `.sops.yaml` encrypts to a dedicated kali-lab age key (in
`~/.config/sops/age/keys.txt`). Project secrets live in `secrets/*.enc.yaml` (committed); the user's
own secrets in `workspace/secrets/*.enc.yaml` (gitignored: user data stays out of the repo even
encrypted). Programs read them with `sops exec-env`; containers get values via `docker exec -e`
or tmpfs, never a plaintext file on a mount. A secret check and gitleaks run in CI. `.ovpn` files
move to encrypted storage too (OPS-3), decrypted into the Lab container's tmpfs at connect time.

**Consequences.** + One rule for everything, nothing readable at rest. − sops/age must be installed and
the key backed up (password manager); losing the key loses the secrets (tokens can be re-issued).
