#!/usr/bin/env bash
# Llamacracy deployment. Idempotent -- safe to re-run after every `git pull`.
#
# Sets up four systemd *user* units (llama-swap, llamacracy, llamacracy-dex,
# llamacracy-auth) and the `llamacracy` CLI that controls all of them at once.
# Scaffolds every local config file from its committed example, filling in
# this checkout's path, your NetBird FQDN and freshly generated secrets.
#
#   ./deploy/install.sh
#   LLAMACRACY_FQDN=myhost.netbird.selfhosted ./deploy/install.sh   # if `netbird status` can't tell us
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
BIN="$HOME/.local/bin"
OAUTH2_PROXY_VERSION="v7.6.0"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

# --- 0. where are we on the NetBird network? ---------------------------------
# The Dex issuer and the oauth2-proxy redirect are pinned to the peer's FQDN
# (stable across reconnects); only the bind address is discovered per start.
fqdn="${LLAMACRACY_FQDN:-}"
if [ -z "$fqdn" ] && command -v netbird >/dev/null 2>&1; then
  fqdn="$(netbird status 2>/dev/null | awk -F': ' '/FQDN/{print $2; exit}' || true)"
fi
if [ -n "$fqdn" ]; then
  say "NetBird FQDN: $fqdn"
else
  warn "could not discover the NetBird FQDN (is NetBird connected?)."
  warn "  re-run as  LLAMACRACY_FQDN=<peer>.netbird.selfhosted ./deploy/install.sh"
  warn "  or edit REPLACE_FQDN by hand in deploy/oauth2-proxy.env + deploy/dex/config.yaml"
fi
fill_fqdn() { [ -n "$fqdn" ] && sed -i "s#REPLACE_FQDN#${fqdn}#g" "$1" || true; }

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

# --- 4. local config files (all gitignored) ----------------------------------
[ -f "$REPO/.env" ] || { cp "$REPO/.env.example" "$REPO/.env"; warn "created .env from example -- set ADMIN_EMAILS and your electricity rates"; }

[ -f "$REPO/bench/inventory.json" ] || {
  cp "$REPO/bench/inventory.example.json" "$REPO/bench/inventory.json"
  warn "created bench/inventory.json from the example -- replace the example models with yours (see bench/README.md)"
}

[ -f "$REPO/deploy/oauth2-proxy.env" ] || {
  cp "$REPO/deploy/oauth2-proxy.env.example" "$REPO/deploy/oauth2-proxy.env"
  fill_fqdn "$REPO/deploy/oauth2-proxy.env"
  secret="$(openssl rand -base64 32 | tr -- '+/' '-_' | tr -d '=')"
  sed -i "s#^OAUTH2_PROXY_COOKIE_SECRET=.*#OAUTH2_PROXY_COOKIE_SECRET=${secret}#" "$REPO/deploy/oauth2-proxy.env"
  warn "created deploy/oauth2-proxy.env with a fresh cookie secret"
}
if [ ! -f "$REPO/deploy/dex/config.yaml" ]; then
  cp "$REPO/deploy/dex/config.yaml.example" "$REPO/deploy/dex/config.yaml"
  fill_fqdn "$REPO/deploy/dex/config.yaml"
  cs="$(openssl rand -hex 32)"
  sed -i "s#^\( *secret:\).*#\1 ${cs}#" "$REPO/deploy/dex/config.yaml"
  sed -i "s#^\(OAUTH2_PROXY_CLIENT_SECRET=\).*#\1${cs}#" "$REPO/deploy/oauth2-proxy.env"
  warn "created deploy/dex/config.yaml with a fresh client secret (matched into oauth2-proxy.env)"
  warn "  -> add a staticPasswords entry per user (deploy/dex/gen-hash.sh 'password'), then:"
  warn "     systemctl --user restart llamacracy-dex"
fi

# --- 5. llama-swap config (generated from the inventory) ---------------------
if [ ! -f "$REPO/config/llama-swap.yaml" ] || [ ! -f "$REPO/config/models.json" ]; then
  ( cd "$REPO" && python3 bench/gen_llamaswap_config.py )
fi
"$BIN/llama-swap" -config "$REPO/config/llama-swap.yaml" -validate

# --- 6. units --------------------------------------------------------------
say "installing systemd user units (repo = $REPO)"
mkdir -p "$UNIT_DIR"
for unit in "$REPO"/deploy/systemd/*.service; do
  sed "s#@REPO@#${REPO}#g" "$unit" > "$UNIT_DIR/$(basename "$unit")"
done
systemctl --user daemon-reload
systemctl --user enable --now llama-swap.service llamacracy.service

# --- 6b. `llamacracy` CLI (up/down/restart/status/logs for the whole stack) --
mkdir -p "$BIN"
ln -sf "$REPO/deploy/llamacracy-cli.sh" "$BIN/llamacracy"
chmod +x "$REPO/deploy/llamacracy-cli.sh"
say "installed: llamacracy up|down|restart|status|logs  (make sure $BIN is on PATH)"

# --- 7. auth: Dex (docker) + oauth2-proxy --------------------------------
if ip -4 -o addr show wt0 >/dev/null 2>&1 && [ -n "$fqdn" ]; then
  addr="$(ip -4 -o addr show wt0 | awk '{print $4}' | cut -d/ -f1)"
  say "NetBird: wt0 = $addr   FQDN = $fqdn"

  # llamacracy-dex.service discovers wt0's address itself on every start
  systemctl --user enable --now llamacracy-dex.service
  dex_ok=false
  if curl -sf -o /dev/null "http://$fqdn:5556/.well-known/openid-configuration"; then
    dex_ok=true
  else
    warn "Dex is running but not reachable yet at http://$fqdn:5556 -- give it a second and recheck:"
    warn "  systemctl --user status llamacracy-dex"
  fi

  secret_set=false
  grep -q '^OAUTH2_PROXY_CLIENT_SECRET=.\+' "$REPO/deploy/oauth2-proxy.env" && secret_set=true
  if grep -q 'REPLACE_FQDN' "$REPO/deploy/oauth2-proxy.env" "$REPO/deploy/dex/config.yaml"; then
    warn "REPLACE_FQDN is still present in deploy/oauth2-proxy.env or deploy/dex/config.yaml -- fix and re-run"
    secret_set=false
  fi

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
  warn "wt0 not present or FQDN unknown -- run 'netbird up' first, then re-run this script"
fi

say "done. Status:  llamacracy status"
