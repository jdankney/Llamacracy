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
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; warn "created .env from example -- fill in rate values (OIDC is via oauth2-proxy)"; }
[ -f "$REPO/deploy/oauth2-proxy.env" ] || {
  cp "$REPO/deploy/oauth2-proxy.env.example" "$REPO/deploy/oauth2-proxy.env"
  secret="$(openssl rand -base64 32 | tr -- '+/' '-_' | tr -d '=')"
  sed -i "s#^OAUTH2_PROXY_COOKIE_SECRET=.*#OAUTH2_PROXY_COOKIE_SECRET=${secret}#" "$REPO/deploy/oauth2-proxy.env"
  warn "created deploy/oauth2-proxy.env with a fresh cookie secret -- add OAUTH2_PROXY_CLIENT_SECRET"
}
if [ ! -f "$REPO/deploy/dex/config.yaml" ]; then
  cp "$REPO/deploy/dex/config.yaml.example" "$REPO/deploy/dex/config.yaml"
  cs="$(openssl rand -hex 32)"
  sed -i "s#^\( *secret:\).*#\1 ${cs}#" "$REPO/deploy/dex/config.yaml"
  sed -i "s#^\(OAUTH2_PROXY_CLIENT_SECRET=\).*#\1${cs}#" "$REPO/deploy/oauth2-proxy.env"
  warn "created deploy/dex/config.yaml with a fresh client secret (matched into oauth2-proxy.env)"
  warn "  -> add a staticPasswords entry per user (deploy/dex/gen-hash.sh 'password'), then:"
  warn "     cd deploy/dex && docker compose up -d"
fi

# --- 5. llama-swap config (from the last benchmark) -------------------------
[ -f "$REPO/config/llama-swap.yaml" ] || ( cd "$REPO" && python3 bench/gen_llamaswap_config.py )
"$BIN/llama-swap" -config "$REPO/config/llama-swap.yaml" -validate

# --- 6. units --------------------------------------------------------------
say "installing systemd user units"
mkdir -p "$UNIT_DIR"
cp "$REPO"/deploy/systemd/*.service "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now llama-swap.service llamacracy.service

# --- 7. auth: Dex (docker) + oauth2-proxy --------------------------------
if ip -4 -o addr show wt0 >/dev/null 2>&1; then
  addr="$(ip -4 -o addr show wt0 | awk '{print $4}' | cut -d/ -f1)"
  fqdn="$(netbird status 2>/dev/null | awk -F': ' '/FQDN/{print $2; exit}')"
  fqdn="${fqdn:-myhost.netbird.selfhosted}"
  say "NetBird: wt0 = $addr   FQDN = $fqdn"

  dex_ok=false
  if [ -f "$REPO/deploy/dex/config.yaml" ]; then
    # llamacracy-dex.service discovers wt0's address itself on every start,
    # so no port-binding patch is needed here -- just (re)start the unit.
    systemctl --user enable --now llamacracy-dex.service
    if curl -sf -o /dev/null "http://$fqdn:5556/.well-known/openid-configuration"; then
      dex_ok=true
    else
      warn "Dex is running but not reachable yet at http://$fqdn:5556 -- give it a second and recheck:"
      warn "  systemctl --user status llamacracy-dex"
    fi
  else
    warn "Dex not configured yet:"
    warn "  cd $REPO/deploy/dex && cp -n config.yaml.example config.yaml && \$EDITOR config.yaml"
    warn "  (set staticPasswords hashes; the client secret is already generated) then:"
    warn "  systemctl --user enable --now llamacracy-dex.service"
  fi

  secret_set=false
  grep -q '^OAUTH2_PROXY_CLIENT_SECRET=.\+' "$REPO/deploy/oauth2-proxy.env" && secret_set=true

  if $dex_ok && $secret_set; then
    systemctl --user enable --now llamacracy-auth.service
    say "auth edge up -- users reach Llamacracy at  http://$fqdn:4180"
  else
    warn "not starting llamacracy-auth yet (need Dex up + OAUTH2_PROXY_CLIENT_SECRET set). Then:"
    warn "  systemctl --user enable --now llamacracy-auth.service"
  fi
  echo
  echo "  Everyone must use  http://$fqdn:4180  -- NOT http://$addr:4180"
  echo "  (the login redirect is pinned to the FQDN; the raw IP will loop)."
else
  warn "wt0 not present -- run 'netbird up' first, then re-run this script"
fi

say "done. Status:  systemctl --user status llama-swap llamacracy llamacracy-auth"
