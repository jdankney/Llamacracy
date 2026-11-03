#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# bcrypt a password for a staticPasswords entry in config.yaml.
#
#   ./gen-hash.sh                     # asks for it, without echoing it
#   ./gen-hash.sh 'their-password'    # also works, but the password then
#                                     # sits in your shell history and `ps`
#
# Needs `htpasswd` (Arch: `pacman -S apache`; Debian: `apt install apache2-utils`).
# The sed rewrites the $2y$ prefix htpasswd emits to $2a$, which Go's bcrypt
# (what Dex uses) accepts unambiguously.
set -euo pipefail

if [ $# -gt 1 ]; then
  echo "usage: $0 ['password']" >&2
  exit 2
fi

if ! command -v htpasswd >/dev/null 2>&1; then
  echo "htpasswd not found -- install apache2-utils / apache" >&2
  exit 1
fi

if [ $# -eq 1 ]; then
  pw="$1"
else
  read -rsp "password: " pw; echo >&2
fi
[ -n "$pw" ] || { echo "empty password" >&2; exit 2; }

# -i: the password goes in on stdin, never on htpasswd's command line
printf '%s' "$pw" | htpasswd -inBC 10 "" | tr -d ':\n' | sed 's/^\$2y/\$2a/'
echo
