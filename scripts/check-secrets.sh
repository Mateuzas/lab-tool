#!/usr/bin/env bash
# Fails if the repo would publish a secret (ADR 006). Runs locally and in CI; needs only git and awk.
#  1. Every tracked file under secrets/ is SOPS-encrypted (all values ENC[...], has a MAC).
#  2. No tracked plaintext secret files (.env, .ovpn, keys, *.dec.*) and nothing from workspace/.
#  3. No private keys or well-known token formats in tracked text.
#  4. If gitleaks is installed, scan the full git history too (default rules + .github/gitleaks.toml).
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
fail=0
bad() { echo "check-secrets: $*" >&2; fail=1; }

# 1. secrets/ must hold only SOPS-encrypted YAML/JSON/dotenv files.
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  case "$f" in
    secrets/*.enc.yaml|secrets/*.enc.json|secrets/*.enc.env) ;;
    *) bad "$f: files in secrets/ must be named *.enc.{yaml,json,env}"; continue ;;
  esac
  grep -q 'mac.*ENC\[AES256_GCM' "$f" || { bad "$f: no SOPS MAC, not encrypted"; continue; }
  case "$f" in
    *.yaml)
      plain=$(awk '/^sops:/{exit} /^[[:space:]]*#/{next} /:[[:space:]]+[^[:space:]]/{
          v=$0; sub(/^[^:]*:[[:space:]]+/, "", v); if (v !~ /^ENC\[/ && v !~ /^[|>]/) print NR": "$0 }' "$f")
      [[ -z "$plain" ]] || bad "$f: plaintext values:"$'\n'"$plain" ;;
    *.json)
      plain=$(grep -nE '"[^"]+": *"' "$f" | grep -vE '"sops"|ENC\[|"(recipient|enc|lastmodified|version|unencrypted_suffix|created_at)"' || true)
      [[ -z "$plain" ]] || bad "$f: possible plaintext values:"$'\n'"$plain" ;;
    *.env)
      plain=$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=' "$f" | grep -vE '=ENC\[|^[0-9]+:sops_' || true)
      [[ -z "$plain" ]] || bad "$f: plaintext values:"$'\n'"$plain" ;;
  esac
done < <(git ls-files secrets)

# 2. Plaintext secret files and user data must never be tracked.
files=$(git ls-files | grep -E '(^|/)\.env(\..*)?$|\.(ovpn|p8|p12|pem|key|agekey)$|(^|/)keys\.txt$|\.dec\.|^workspace/' || true)
[[ -z "$files" ]] || bad "tracked plaintext secret files or user data:"$'\n'"$files"

# 3. Key material and common token formats in tracked text files.
pattern='AGE-SECRET-KEY-1|sk-ant-[A-Za-z0-9_-]{20,}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|<key>|<tls-auth>|<tls-crypt>|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{32,}|xox[abpr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|INTIGRITI_TOKEN *[:=] *[A-Za-z0-9._-]{20,}'
hits=$(git grep -nIE "$pattern" -- . ':!scripts/check-secrets.sh' ':!.github/gitleaks.toml' || true)
[[ -z "$hits" ]] || bad "secret-looking strings:"$'\n'"$hits"

# 4. Optional full-history scan.
# Scans a temporary clone: on an iCloud-synced folder (this repo on ~/Desktop) gitleaks hangs on
# the working tree's files, while a clone outside it takes well under a second.
if command -v gitleaks >/dev/null; then
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/check-secrets.XXXXXX")
  if git clone -q --no-local . "$tmp/repo" 2>/dev/null; then
    gitleaks git --config .github/gitleaks.toml --redact --no-banner --log-level warn "$tmp/repo" >&2 \
      || bad "gitleaks found leaks in history"
  else
    bad "could not clone the repo for gitleaks"
  fi
  rm -rf "$tmp"
fi

if (( fail )); then exit 1; fi
echo "check-secrets: ok"
