#!/usr/bin/env bash
# bcrypt a password for a staticPasswords entry in config.yaml.
#
#   ./gen-hash.sh 'their-password'
#
# Needs `htpasswd` (Arch: `pacman -S apache`; Debian: `apt install apache2-utils`).
# The sed rewrites the $2y$ prefix htpasswd emits to $2a$, which Go's bcrypt
# (what Dex uses) accepts unambiguously.
set -euo pipefail

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 'password'" >&2
  exit 2
fi

if ! command -v htpasswd >/dev/null 2>&1; then
  echo "htpasswd not found -- install apache2-utils / apache" >&2
  exit 1
fi

htpasswd -bnBC 10 "" "$1" | tr -d ':\n' | sed 's/^\$2y/\$2a/'
echo
