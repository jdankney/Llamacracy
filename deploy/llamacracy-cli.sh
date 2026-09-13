#!/usr/bin/env bash
# Llamacracy stack control -- one command for the whole thing instead of
# remembering four systemd --user unit names. Installed on PATH as
# `llamacracy` by deploy/install.sh (symlinked here, so `git pull` + a re-run
# of install.sh always gives you the latest version).
#
#   llamacracy up        start everything (dex -> llama-swap -> app -> auth edge)
#   llamacracy down       stop everything
#   llamacracy restart    down then up
#   llamacracy status     one line per unit + a quick reachability check
#   llamacracy logs [swap|dex|app|auth]   follow logs (default: all, interleaved)
#
# systemd resolves the actual dependency order itself (each unit's own
# After=/Wants=, see deploy/systemd/*.service) regardless of the order units
# are listed on the command line -- so `up`/`down` just hand it the full set.
set -euo pipefail

UNITS=(llamacracy-dex.service llama-swap.service llamacracy.service llamacracy-auth.service)

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

usage() {
  cat <<EOF
Llamacracy stack control.

Usage: llamacracy <command>

  up                start the whole stack
  down              stop the whole stack
  restart           down then up
  status            one line per unit + a quick reachability check
  logs [target]     follow logs -- target is swap|dex|app|auth (default: all)

Units: ${UNITS[*]}
EOF
}

cmd_up() {
  say "starting: ${UNITS[*]}"
  systemctl --user start "${UNITS[@]}"
  # `start` returns as soon as systemd considers each unit launched -- the app
  # (OIDC discovery, etc.) and the auth edge still take a beat to actually
  # come up, so give them a few seconds before judging readiness.
  for _ in 1 2 3 4 5 6 7 8; do
    systemctl --user is-active --quiet llamacracy-auth.service && \
      curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8000/healthz && break
    sleep 1
  done
  cmd_status
}

cmd_down() {
  say "stopping: ${UNITS[*]}"
  systemctl --user stop "${UNITS[@]}"
  cmd_status
}

cmd_restart() {
  say "restarting: ${UNITS[*]}"
  systemctl --user restart "${UNITS[@]}"
  cmd_status
}

cmd_status() {
  for u in "${UNITS[@]}"; do
    state="$(systemctl --user is-active "$u" 2>/dev/null || true)"
    if [ "$state" = "active" ]; then
      printf '  \033[1;32m●\033[0m %-24s active\n' "$u"
    else
      printf '  \033[1;31m●\033[0m %-24s %s\n' "$u" "${state:-unknown}"
    fi
  done
  if systemctl --user is-active --quiet llamacracy.service; then
    if curl -sf -o /dev/null --max-time 3 http://127.0.0.1:8000/healthz; then
      echo "  app healthz: ok"
    else
      warn "app service is active but /healthz didn't respond"
    fi
  fi
}

cmd_logs() {
  local target
  case "${1:-all}" in
    swap) target=llama-swap.service ;;
    dex) target=llamacracy-dex.service ;;
    app) target=llamacracy.service ;;
    auth) target=llamacracy-auth.service ;;
    all)
      args=()
      for u in "${UNITS[@]}"; do args+=(-u "$u"); done
      exec journalctl --user "${args[@]}" -f
      ;;
    *)
      echo "unknown log target: $1 (want swap|dex|app|auth|all)" >&2
      exit 1
      ;;
  esac
  exec journalctl --user -u "$target" -f
}

case "${1:-}" in
  up) cmd_up ;;
  down) cmd_down ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "${1:-all}" ;;
  -h|--help|help|"") usage ;;
  *) echo "unknown command: $1" >&2; usage; exit 1 ;;
esac
