#!/usr/bin/env bash
# ==============================================================================
# Antigravity Webhook Hub — Ephemeral Cloudflare Quick Tunnel
# ==============================================================================
# Starts a zero-configuration ephemeral tunnel on trycloudflare.com pointing
# to the local webhook hub (default port 9423).
#
# Commands:
#   ./tunnel/quick_tunnel.sh          Start quick tunnel and print public URL
#   ./tunnel/quick_tunnel.sh --stop   Stop running quick tunnel
#   ./tunnel/quick_tunnel.sh --status Check quick tunnel status & public URL
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PID_FILE="/tmp/antigravity_quick_tunnel.pid"
LOG_FILE="/tmp/antigravity_quick_tunnel.log"
URL_FILE="/tmp/antigravity_quick_tunnel.url"

# Source .env if available (portable dot-sourcing for macOS bash 3.2 compatibility)
if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ROOT_DIR}/.env"
  set +a
fi

PORT="${PORT:-9423}"

# Parse command line options
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop|-s)
      if [[ -f "${PID_FILE}" ]]; then
        PID=$(cat "${PID_FILE}")
        if ps -p "${PID}" >/dev/null 2>&1; then
          echo "Stopping Cloudflare quick tunnel (PID ${PID})..."
          kill "${PID}" || true
          rm -f "${PID_FILE}" "${URL_FILE}"
          echo "Quick tunnel stopped."
        else
          echo "Process ${PID} is not running. Cleaning up stale PID file."
          rm -f "${PID_FILE}" "${URL_FILE}"
        fi
      else
        echo "No active quick tunnel found (no ${PID_FILE})."
      fi
      exit 0
      ;;
    --status)
      if [[ -f "${PID_FILE}" ]]; then
        PID=$(cat "${PID_FILE}")
        if ps -p "${PID}" >/dev/null 2>&1; then
          URL=$(cat "${URL_FILE}" 2>/dev/null || echo "Unknown")
          echo "Quick tunnel is RUNNING (PID ${PID})"
          echo "Public URL: ${URL}"
          echo "Webhook Endpoint: ${URL}/webhook"
          exit 0
        fi
      fi
      echo "Quick tunnel is NOT running."
      exit 1
      ;;
    --port|-p)
      PORT="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --port, -p <PORT>   Local port to proxy (default: 9423)"
      echo "  --stop, -s          Stop any running quick tunnel"
      echo "  --status            Check quick tunnel status and public URL"
      echo "  -h, --help          Show this message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Check cloudflared binary
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "Error: 'cloudflared' command not found."
  echo "Please install it via Homebrew: brew install cloudflared"
  exit 1
fi

# If already running, notify user
if [[ -f "${PID_FILE}" ]]; then
  EXISTING_PID=$(cat "${PID_FILE}")
  if ps -p "${EXISTING_PID}" >/dev/null 2>&1; then
    EXISTING_URL=$(cat "${URL_FILE}" 2>/dev/null || echo "Unknown")
    echo "A quick tunnel is already running with PID ${EXISTING_PID}."
    echo "Public URL: ${EXISTING_URL}"
    echo "To restart, run: $0 --stop && $0"
    exit 0
  fi
fi

echo "=================================================================="
echo "Launching Cloudflare Ephemeral Quick Tunnel..."
echo "Targeting Local Ingress: http://127.0.0.1:${PORT}"
echo "=================================================================="

# Clear old logs
rm -f "${LOG_FILE}" "${URL_FILE}"

# Start cloudflared in background
cloudflared tunnel --url "http://127.0.0.1:${PORT}" > "${LOG_FILE}" 2>&1 &
TUNNEL_PID=$!
echo "${TUNNEL_PID}" > "${PID_FILE}"

# Poll log file for assigned trycloudflare.com URL
echo "Waiting for public HTTPS tunnel URL to be assigned..."
MAX_WAIT_SECONDS=30
ASSIGNED_URL=""

for ((i=1; i<=MAX_WAIT_SECONDS; i++)); do
  if ! ps -p "${TUNNEL_PID}" >/dev/null 2>&1; then
    echo "Error: cloudflared process terminated unexpectedly."
    cat "${LOG_FILE}"
    rm -f "${PID_FILE}"
    exit 1
  fi

  # Search for trycloudflare URL in logs
  ASSIGNED_URL=$(grep -o "https://[a-zA-Z0-9-]*\.trycloudflare\.com" "${LOG_FILE}" | head -n 1 || true)
  if [[ -n "${ASSIGNED_URL}" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "${ASSIGNED_URL}" ]]; then
  echo "Error: Timed out waiting for Cloudflare trycloudflare.com URL after ${MAX_WAIT_SECONDS}s."
  echo "Logs from ${LOG_FILE}:"
  cat "${LOG_FILE}"
  kill "${TUNNEL_PID}" 2>/dev/null || true
  rm -f "${PID_FILE}"
  exit 1
fi

echo "${ASSIGNED_URL}" > "${URL_FILE}"

echo "=================================================================="
echo "Tunnel Established Successfully!"
echo "Tunnel PID:     ${TUNNEL_PID}"
echo "Public Gateway: ${ASSIGNED_URL}"
echo "Webhook Ingress: ${ASSIGNED_URL}/webhook"
echo "Health Check:    ${ASSIGNED_URL}/healthz"
echo "=================================================================="
echo "To stop this tunnel, run: $0 --stop"
