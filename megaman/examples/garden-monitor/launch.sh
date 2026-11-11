#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="garden-monitor"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${PROJECT_DIR}/launch.log"
PORT=8000
SUDO=

# ── Startup ──────────────────────────────────────────────────────────────
echo "[$(date -Iseconds)] Starting ${PROJECT_NAME}..." | tee -a "$LOG_FILE"

# Launch command. Use 'exec' so the process replaces the shell (MegaMan tracks
# the PID). All output must go to $LOG_FILE. $SUDO expands to "sudo" or "".
exec $SUDO python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" >> "$LOG_FILE" 2>&1
