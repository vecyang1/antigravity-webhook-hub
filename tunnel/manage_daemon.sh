#!/usr/bin/env bash
# Antigravity Webhook Hub - Daemon Management Forwarder
# Delegates to native CLI: ./bin/webhook-hub service / start / stop
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN="$PROJECT_DIR/bin/webhook-hub"

ACTION="${1:-status}"

case "$ACTION" in
  start|enable-autostart)
    "$BIN" service install
    ;;
  stop|disable-autostart)
    "$BIN" service uninstall
    ;;
  restart)
    "$BIN" service restart
    ;;
  status)
    "$BIN" service status
    ;;
  logs)
    shift || true
    "$BIN" service logs "$@"
    ;;
  run)
    exec "$BIN" start --host 127.0.0.1 --port 9423 --db data/webhook_hub.db --pidfile .webhook-hub.pid
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs|enable-autostart|disable-autostart|run}"
    exit 1
    ;;
esac
