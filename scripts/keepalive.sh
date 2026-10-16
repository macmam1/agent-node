#!/bin/bash
# keep-alive script for sleep-prone platforms
# Runs as a background process to prevent container from sleeping
#
# Usage: ./keepalive.sh &
# or: nohup ./keepalive.sh &

TARGET="${1:-http://localhost:${PORT:-8080}/health}"
INTERVAL="${2:-300}"  # 5 minutes
LOGFILE="/var/log/agent-node/keepalive.log"

echo "[$(date)] keep-alive started — target: $TARGET, interval: ${INTERVAL}s" >> "$LOGFILE"

while true; do
    STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "$TARGET" 2>/dev/null || echo "000")
    echo "[$(date)] Health check: $STATUS" >> "$LOGFILE"

    # If container is sleeping, health check will fail
    # This ping itself wakes the container
    if [ "$STATUS" = "000" ]; then
        echo "[$(date)] WARNING: Container appears to be sleeping" >> "$LOGFILE"
    fi

    sleep "$INTERVAL"
done
