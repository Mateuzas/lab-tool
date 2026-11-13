#!/usr/bin/env bash
# Manage kali-lab secrets with SOPS + age. See docs/SECRETS.md (ADR 006).
#
#   scripts/secrets.sh edit <name>            open workspace/secrets/<name>.enc.yaml (your own secrets)
#   scripts/secrets.sh edit --project <name>  open secrets/<name>.enc.yaml (committed project secrets)
#   scripts/secrets.sh exec <name> -- <cmd>   run <cmd> with the secret's keys as environment variables
#   scripts/secrets.sh list                   which secrets exist (names and keys, never values)
#   scripts/secrets.sh rekey                  re-encrypt everything after .sops.yaml recipients change
#   scripts/secrets.sh check                  run scripts/check-secrets.sh
#
# A new file starts from scripts/secret-templates/<name>.yaml if there is one.
# Decryption needs the age private key in $SOPS_AGE_KEY_FILE (or $SOPS_AGE_KEY).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1

die() { echo "secrets: $*" >&2; exit 1; }
need_sops() { command -v sops >/dev/null || die "sops not found (brew install sops age)"; }

check_name() {
  [[ "${1:-}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "name must be lowercase letters, digits, - or _ (got '${1:-}')"
}

# Your own secrets live in workspace/secrets/ (gitignored); project secrets in secrets/ (committed).
secret_path() {
  local dir=workspace/secrets
  if [[ "${1:-}" == --project ]]; then dir=secrets; shift; fi
  local name="${1:-}"
  name="${name#workspace/}"; name="${name#secrets/}"; name="${name%.enc.yaml}"
  check_name "$name"   # rejects anything with a / in it, so no path can escape the folder
  echo "$dir/$name.enc.yaml"
}

cmd_edit() {
  need_sops
  local f name tpl
  f="$(secret_path "$@")"
  name="$(basename "$f" .enc.yaml)"; tpl="scripts/secret-templates/$name.yaml"
  mkdir -p "$(dirname "$f")"
  if [[ ! -e "$f" && -f "$tpl" ]]; then
    # Encrypt the template (placeholders only) straight from the repo; nothing plaintext is written.
    umask 077
    sops encrypt --filename-override "$f" "$tpl" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "created $f from $tpl"
  fi
  # `sops edit` on a missing file opens a template and encrypts it on save. Exit 200 means
  # "file has not changed", which is fine.
  local rc=0
  sops edit "$f" || rc=$?
  (( rc == 0 || rc == 200 )) || exit "$rc"
}

cmd_exec() {
  need_sops
  local f
  f="$(secret_path "${1:-}")"; shift
  [[ "${1:-}" == -- ]] && shift
  [[ $# -gt 0 ]] || die "usage: scripts/secrets.sh exec <name> -- <command...>"
  [[ -f "$f" ]] || die "$f missing: create it with scripts/secrets.sh edit $(basename "$f" .enc.yaml)"
  # Values reach the command's environment only; sops quotes nothing into a shell string.
  local cmd; printf -v cmd '%q ' "$@"
  exec sops exec-env "$f" "$cmd"
}

all_files() {
  local f
  for f in secrets/*.enc.{yaml,json,env} workspace/secrets/*.enc.{yaml,json,env}; do
    [[ -e "$f" ]] && echo "$f"
  done
  return 0
}

cmd_list() {
  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    printf '%s: %s\n' "$f" "$(awk '/^sops:/{exit} /^[A-Za-z_][A-Za-z0-9_]*:/{sub(/:.*/,""); printf "%s ", $0}' "$f")"
  done < <(all_files)
}

cmd_rekey() {
  need_sops
  local f found=0
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    found=1; sops updatekeys --yes "$f"
  done < <(all_files)
  (( found )) || echo "no secrets files to rekey"
}

case "${1:-}" in
  edit)  shift; cmd_edit "$@" ;;
  exec)  shift; cmd_exec "$@" ;;
  list)  cmd_list ;;
  rekey) cmd_rekey ;;
  check) exec scripts/check-secrets.sh ;;
  *)     sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
