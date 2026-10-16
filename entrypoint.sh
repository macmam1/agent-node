#!/bin/bash
set -e

# ─── agent-node Entrypoint ───────────────────────────────────────
echo "[entrypoint] agent-node v2.1 starting..."

# Ensure log directory exists
mkdir -p /var/log/agent-node

# Set defaults
export PORT="${PORT:-8080}"
export HOST="${HOST:-0.0.0.0}"
export TERM="${TERM:-xterm-256color}"
export PYTHONUNBUFFERED=1

# Generate MONITOR_TOKEN if not set (for API auth)
if [ -z "${MONITOR_TOKEN}" ]; then
    EXPORT_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    export MONITOR_TOKEN="$EXPORT_TOKEN"
    echo "[entrypoint] MONITOR_TOKEN auto-generated — use this for /api/* and /ws/* auth:"
    echo "[entrypoint] TOKEN: $MONITOR_TOKEN"
fi

# Log environment summary
echo "[entrypoint] Port: $PORT"
echo "[entrypoint] Host: $HOST"
echo "[entrypoint] SSH client: $(which ssh 2>/dev/null || echo 'not found')"
echo "[entrypoint] curl: $(which curl 2>/dev/null || echo 'not found')"
echo "[entrypoint] xray: ${XRAY_ENABLED:-false}"

# Run startup scripts
for script in /app/scripts/setup_ssh.sh /app/scripts/install_xray.sh; do
    if [ -x "$script" ]; then
        echo "[entrypoint] Running: $script"
        if [ "$script" = "/app/scripts/install_xray.sh" ] && [ "${XRAY_ENABLED:-false}" != "true" ]; then
            echo "[entrypoint] Skipping xray install (XRAY_ENABLED != true)"
        else
            bash "$script" || echo "[entrypoint] WARNING: $script failed"
        fi
    fi
done

# Start the application
echo "[entrypoint] Starting Python application..."
exec python3 /app/app.py
