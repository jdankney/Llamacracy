#!/usr/bin/env bash
# Llamacracy deployment (Phase 6). Idempotent -- safe to re-run.
# Sets up three systemd *user* units: llama-swap, llamacracy, llamacracy-auth.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
BIN="$HOME/.local/bin"
OAUTH2_PROXY_VERSION="v7.6.0"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

# --- 1. linger so the units run without an active login session ---------------
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  say "enabling linger (needs sudo once)"
  sudo loginctl enable-linger "$USER"
fi

# --- 2. python env -----------------------------------------------------------
say "syncing python env"
( cd "$REPO" && uv sync --frozen )

# --- 3. oauth2-proxy binary --------------------------------------------------
if [ ! -x "$BIN/oauth2-proxy" ]; then
  say "installing oauth2-proxy $OAUTH2_PROXY_VERSION"
  mkdir -p "$BIN"
  tmp="$(mktemp -d)"
  curl -sSL -o "$tmp/o.tar.gz" \
    "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/${OAUTH2_PROXY_VERSION}/oauth2-proxy-${OAUTH2_PROXY_VERSION}.linux-amd64.tar.gz"
  tar -xzf "$tmp/o.tar.gz" -C "$tmp" --strip-components=1
  install -m755 "$tmp/oauth2-proxy" "$BIN/oauth2-proxy"
  rm -rf "$tmp"
fi

# --- 4. config files --------------------------------------------------------
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; warn "created .env from example -- fill in OIDC + rate values"; }
[ -f "$REPO/deploy/oauth2-proxy.env" ] || {
  cp "$REPO/deploy/oauth2-proxy.env.example" "$REPO/deploy/oauth2-proxy.env"
  secret="$(openssl rand -base64 32 | tr -- '+/' '-_' | tr -d '=')"
  sed -i "s#^OAUTH2_PROXY_COOKIE_SECRET=.*#OAUTH2_PROXY_COOKIE_SECRET=${secret}#" "$REPO/deploy/oauth2-proxy.env"
  warn "created deploy/oauth2-proxy.env with a fresh cookie secret -- add CLIENT_ID / CLIENT_SECRET"
}

# --- 5. llama-swap config (from the last benchmark) -------------------------
[ -f "$REPO/config/llama-swap.yaml" ] || ( cd "$REPO" && python3 bench/gen_llamaswap_config.py )
"$BIN/llama-swap" -config "$REPO/config/llama-swap.yaml" -validate

# --- 6. units --------------------------------------------------------------
say "installing systemd user units"
mkdir -p "$UNIT_DIR"
cp "$REPO"/deploy/systemd/*.service "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now llama-swap.service llamacracy.service

# --- 7. auth: show the redirect URI the IdP needs, then start -------------
if ip -4 -o addr show wt0 >/dev/null 2>&1; then
  addr="$(ip -4 -o addr show wt0 | awk '{print $4}' | cut -d/ -f1)"
  say "NetBird interface wt0 = $addr"
  echo
  echo "  Register this redirect URI in the NetBird IdP, then put CLIENT_ID/SECRET"
  echo "  in deploy/oauth2-proxy.env:"
  echo
  echo "      http://$addr:4180/oauth2/callback"
  echo
  if grep -q '^OAUTH2_PROXY_CLIENT_ID=.\+' "$REPO/deploy/oauth2-proxy.env"; then
    systemctl --user enable --now llamacracy-auth.service
    say "auth edge up at http://$addr:4180"
  else
    warn "skipping llamacracy-auth.service until CLIENT_ID is set. Then:"
    warn "  systemctl --user enable --now llamacracy-auth.service"
  fi
else
  warn "wt0 not present -- run 'netbird up' first, then re-run this script"
fi

say "done. Status:  systemctl --user status llama-swap llamacracy llamacracy-auth"
