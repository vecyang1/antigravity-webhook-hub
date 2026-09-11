#!/usr/bin/env bash
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$PROJECT_DIR/.webhook-hub.pid"
LOG_FILE="$PROJECT_DIR/webhook-hub.log"
ERR_FILE="$PROJECT_DIR/webhook-hub-err.log"
PYTHON_BIN="/opt/homebrew/bin/python3"
PLIST_NAME="com.vec.antigravity-webhook-hub.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"
SRC_PLIST="$PROJECT_DIR/tunnel/$PLIST_NAME"

case "$1" in
  run)
    export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    export PYTHONUNBUFFERED=1
    cd "$PROJECT_DIR" || exit 1
    # Clean stale PID file if process not running
    if [ -f "$PID_FILE" ]; then
      OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
      if [ -n "$OLD_PID" ] && ! kill -0 "$OLD_PID" 2>/dev/null; then
        rm -f "$PID_FILE"
      fi
    fi
    exec "$PYTHON_BIN" -B "$PROJECT_DIR/bin/webhook-hub" start --host 127.0.0.1 --port 9423 --db data/webhook_hub.db --pidfile .webhook-hub.pid
    ;;
  start)
    $0 enable-autostart
    ;;
  stop)
    $0 disable-autostart
    ;;
  status)
    PID=$(pgrep -f "webhook-hub start" | head -n 1)
    if [ -n "$PID" ]; then
      echo "Antigravity Webhook Hub is RUNNING (PID $PID)"
    else
      echo "Antigravity Webhook Hub is STOPPED"
    fi
    ;;
  restart)
    $0 stop
    sleep 1
    $0 start
    ;;
  enable-autostart)
    echo "Enabling macOS auto-start on login (LaunchAgent)..."
    "$PROJECT_DIR/bin/webhook-hub" stop 2>/dev/null || true
    pkill -f "webhook-hub" 2>/dev/null || true
    sleep 1
    rm -f "$PID_FILE"
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$SRC_PLIST" "$TARGET_PLIST"
    launchctl unload "$TARGET_PLIST" 2>/dev/null || true
    launchctl load -w "$TARGET_PLIST"
    sleep 2
    PID=$(pgrep -f "webhook-hub start" | head -n 1)
    if [ -n "$PID" ]; then
      echo "Auto-start enabled and daemon RUNNING (PID $PID)!"
    else
      echo "Warning: Daemon launched via LaunchAgent, check $LOG_FILE or $ERR_FILE"
    fi
    ;;
  disable-autostart)
    echo "Disabling macOS auto-start..."
    launchctl unload "$TARGET_PLIST" 2>/dev/null || true
    rm -f "$TARGET_PLIST"
    "$PROJECT_DIR/bin/webhook-hub" stop 2>/dev/null || true
    pkill -f "webhook-hub" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "Daemon stopped and auto-start disabled."
    ;;
  *)
    echo "Usage: $0 {run|start|stop|restart|status|enable-autostart|disable-autostart}"
    exit 1
    ;;
esac
