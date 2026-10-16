#!/bin/bash
set -e

# ─── agent-node Entrypoint ───────────────────────────────────────
# Initializes the environment and starts the monitoring service

echo "[entrypoint] agent-node v2.0 starting..."

# Ensure log directory exists
mkdir -p /var/log/agent-node

# Set defaults if not provided by platform
export PORT="${PORT:-8080}"
export HOST="${HOST:-0.0.0.0}"
export TERM="${TERM:-xterm-256color}"

# Log environment summary (no secrets)
echo "[entrypoint] Port: $PORT"
echo "[entrypoint] Host: $HOST"
echo "[entrypoint] PID: $$"
echo "[entrypoint] Python: $(python3 --version)"
echo "[entrypoint] SSH client: $(which ssh 2>/dev/null || echo 'not found')"
echo "[entrypoint] curl: $(which curl 2>/dev/null || echo 'not found')"
echo "[entrypoint] XRAY_ENABLED: ${XRAY_ENABLED:-false}"

# If xray binary exists and is enabled, fetch config
if [ "${XRAY_ENABLED:-false}" = "true" ] && [ -f /app/bin/agent ]; then
    echo "[entrypoint] xray configured but not started — use /api/xray/start"
fi

# If MONITOR_TOKEN not set, generate one
if [ -z "${MONITOR_TOKEN}" ]; then
    export MONITOR_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '\n' | head -c 32)
    echo "[entrypoint] Generated MONITOR_TOKEN (set MONITOR_TOKEN env var to change)"
fi

# Run health check pre-flight
echo "[entrypoint] Health check URL: http://localhost:${PORT}/health"

# Start the application
echo "[entrypoint] Starting application..."
exec python3 /app/app.py
