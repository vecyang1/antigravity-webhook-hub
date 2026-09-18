#!/usr/bin/env bash
# ==============================================================================
# Antigravity Webhook Hub — Cloudflare Named Tunnel Runner
# ==============================================================================
# Starts a production named Cloudflare Tunnel routing external traffic to
# the local asyncio webhook gateway on port 9423 without opening router ports.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source .env if available (portable dot-sourcing for macOS bash 3.2 compatibility)
if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ROOT_DIR}/.env"
  set +a
fi

DEFAULT_CONFIG_FILE="${SCRIPT_DIR}/config.yml"
CONFIG_FILE="${DEFAULT_CONFIG_FILE}"
TEMPLATE_FILE="${SCRIPT_DIR}/config.yml.template"

print_usage() {
  echo "Usage: $0 [OPTIONS] [CONFIG_PATH]"
  echo ""
  echo "Options:"
  echo "  --status        Check running tunnel status, local gateway, and public ingress"
  echo "  --check         Validate cloudflared installation and config file existence"
  echo "  --init          Generate tunnel/config.yml from tunnel/config.yml.template"
  echo "  --foreground    Force run standalone cloudflared even if LaunchDaemon is running"
  echo "  -h, --help      Display this help message"
  echo ""
  echo "Environment Variables (from .env or shell):"
  echo "  CLOUDFLARE_TUNNEL_NAME        Name of the named tunnel (default: 26.03.08)"
  echo "  CLOUDFLARE_TUNNEL_HOSTNAME    Public hostname (default: webhook.worldinspirelab.com)"
  echo "  PORT                          Local port to route ingress to (default: 9423)"
}

generate_config() {
  echo "Generating ${CONFIG_FILE} from template ${TEMPLATE_FILE}..."
  local tunnel_name="${CLOUDFLARE_TUNNEL_NAME:-26.03.08}"
  local tunnel_hostname="${CLOUDFLARE_TUNNEL_HOSTNAME:-webhook.worldinspirelab.com}"
  local creds_file="${CLOUDFLARE_CREDENTIALS_FILE:-~/.cloudflared/${tunnel_name}.json}"
  local port="${PORT:-9423}"

  sed -e "s|\${CLOUDFLARE_TUNNEL_NAME:-antigravity-webhook-tunnel}|${tunnel_name}|g" \
      -e "s|\${CLOUDFLARE_TUNNEL_HOSTNAME:-webhook.yourdomain.com}|${tunnel_hostname}|g" \
      -e "s|\${CLOUDFLARE_CREDENTIALS_FILE:-~/.cloudflared/antigravity-webhook-tunnel.json}|${creds_file}|g" \
      -e "s|\${PORT:-9423}|${port}|g" \
      "${TEMPLATE_FILE}" > "${CONFIG_FILE}"

  echo "Generated config at ${CONFIG_FILE}"
}

# Check cloudflared binary
check_binary() {
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "Error: 'cloudflared' command not found."
    echo "On macOS, install it using Homebrew:"
    echo "  brew install cloudflared"
    exit 1
  fi
}

ACTION="run"
FORCE_FOREGROUND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --init)
      ACTION="init"
      shift
      ;;
    --check)
      ACTION="check"
      shift
      ;;
    --status)
      ACTION="status"
      shift
      ;;
    --foreground|-f)
      FORCE_FOREGROUND=1
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1"
      print_usage
      exit 1
      ;;
    *)
      CONFIG_FILE="$1"
      shift
      ;;
  esac
done

check_binary

if [[ "${ACTION}" == "init" ]]; then
  generate_config
  exit 0
fi

if [[ "${ACTION}" == "check" ]]; then
  echo "Cloudflared version: $(cloudflared --version)"
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "Warning: Config file ${CONFIG_FILE} does not exist. Run '$0 --init' to generate it."
    exit 1
  fi
  echo "Config file present: ${CONFIG_FILE}"
  cloudflared tunnel --config "${CONFIG_FILE}" ingress validate
  exit 0
fi

if [[ "${ACTION}" == "status" ]]; then
  echo "=================================================================="
  echo " Cloudflare Tunnel Status Check"
  echo "=================================================================="
  PUBLIC_HOST="${CLOUDFLARE_TUNNEL_HOSTNAME:-webhook.worldinspirelab.com}"
  LOCAL_PORT="${PORT:-9423}"

  RUNNING_PID=$(pgrep -f "cloudflared.*tunnel" | head -n 1 || true)
  if [[ -n "${RUNNING_PID}" ]]; then
    echo "  Process:       RUNNING (PID ${RUNNING_PID})"
    if [[ -f "/Library/LaunchDaemons/com.cloudflare.cloudflared.plist" ]]; then
      echo "  Daemon:        macOS LaunchDaemon (/Library/LaunchDaemons/com.cloudflare.cloudflared.plist)"
    fi
    HTTP2_AGENT_PID=$(launchctl list 2>/dev/null | grep "com.vec.cloudflared-http2" | awk '{print $1}' || true)
    if [[ -n "${HTTP2_AGENT_PID}" && "${HTTP2_AGENT_PID}" != "-" ]]; then
      echo "  HTTP/2 Agent:  RUNNING (PID ${HTTP2_AGENT_PID}, Protocol: HTTP/2)"
    fi
  else
    echo "  Process:       STOPPED (No active cloudflared tunnel process)"
  fi

  echo "  Local Gateway: http://127.0.0.1:${LOCAL_PORT}"
  if curl -s -f "http://127.0.0.1:${LOCAL_PORT}/healthz" >/dev/null 2>&1; then
    echo "  Local Health:  ONLINE (200 OK)"
  else
    echo "  Local Health:  OFFLINE"
  fi

  echo "  Public Ingress: https://${PUBLIC_HOST}"
  PUBLIC_HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 "https://${PUBLIC_HOST}/healthz" 2>/dev/null || echo "000")
  if [[ "${PUBLIC_HTTP_CODE}" == "200" ]]; then
    echo "  Public Health: ONLINE (200 OK reachable via Cloudflare Edge)"
  else
    echo "  Public Health: UNREACHABLE (HTTP ${PUBLIC_HTTP_CODE})"
    EDGE_IP=$(dig +short region1.v2.argotunnel.com 2>/dev/null | head -n 1 || true)
    if [[ "${EDGE_IP}" =~ ^198\.18\. ]]; then
      echo "  ↳ ⚠️ Warning: Cloudflare Edge resolved to Fake-IP (${EDGE_IP}) via TUN/Proxy. Disable TUN or add bypass."
    fi
  fi
  echo "=================================================================="
  exit 0
fi

# Auto-generate config.yml if missing and template exists
if [[ ! -f "${CONFIG_FILE}" && -f "${TEMPLATE_FILE}" ]]; then
  generate_config
fi

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Error: Configuration file not found at: ${CONFIG_FILE}"
  echo "Generate one using: $0 --init"
  exit 1
fi

RUNNING_PID=$(pgrep -f "cloudflared.*tunnel" | head -n 1 || true)
if [[ -n "${RUNNING_PID}" && "${FORCE_FOREGROUND}" -eq 0 ]]; then
  PUBLIC_HOST="${CLOUDFLARE_TUNNEL_HOSTNAME:-webhook.worldinspirelab.com}"
  echo "=================================================================="
  echo "Cloudflare Tunnel is ALREADY RUNNING (PID ${RUNNING_PID})"
  echo "Managed via LaunchDaemon: /Library/LaunchDaemons/com.cloudflare.cloudflared.plist"
  echo "Public Host:   https://${PUBLIC_HOST}"
  echo "Target Port:   http://127.0.0.1:${PORT:-9423}"
  echo "To view status: $0 --status"
  echo "To force foreground run: $0 --foreground"
  echo "=================================================================="
  exit 0
fi

echo "=================================================================="
echo "Starting Cloudflare Named Tunnel..."
echo "Config: ${CONFIG_FILE}"
echo "Local Ingress Target: http://127.0.0.1:${PORT:-9423}"
echo "=================================================================="

exec cloudflared tunnel --config "${CONFIG_FILE}" run
