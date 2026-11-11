# Launch Configuration: garden-monitor

## Overview
This project uses a standardized `launch.sh` script managed by MegaMan (the project bot).
MegaMan will execute, monitor, and manage the running process — **do not change the contract below**.

## Launch Type
- **Type:** Web server
- **Port:** 8000 (auto-assigned — unique per project)
- **Health endpoint:** `GET /health` must return HTTP 200

## Sudo Requirements
- **Default:** none. `SUDO=` is empty, so launch.sh runs as the normal user.
- **If this project needs root:** prefer putting that work in
  `tools/setup.sh` and approving it once (`/sudo action:approve project:garden-monitor script:tools/setup.sh`), so the long-running process itself
  stays unprivileged.
- **If the launch itself must be root** (binds a port <1024, needs device
  access): set `SUDO=sudo` in launch.sh, use `$SUDO` before the exec line,
  and ask the owner for `/sudo action:approve project:garden-monitor script:launch.sh`.
- Sessions have **no** general sudo, and editing an approved script cancels
  its approval — ask again after changes.

## launch.sh Contract

The `launch.sh` script in the project root **must** follow these rules exactly:

### Structure
```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="garden-monitor"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${PROJECT_DIR}/launch.log"
PORT=8000

# ── Startup ──────────────────────────────────────────────────────────────
echo "[$(date -Iseconds)] Starting ${PROJECT_NAME}..." | tee -a "$LOG_FILE"

# Your actual launch command goes here.
# All stdout and stderr MUST be appended to $LOG_FILE.
# Example for a web server:
#   exec python -m uvicorn main:app --host 0.0.0.0 --port "$PORT" >> "$LOG_FILE" 2>&1
# Example for a CLI tool:
#   exec python main.py "$@" >> "$LOG_FILE" 2>&1

exec $SUDO python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT" >> "$LOG_FILE" 2>&1
```

### Required Conventions
1. **Shebang**: `#!/usr/bin/env bash` — always.
2. **`set -euo pipefail`** — fail fast on errors.
3. **`PROJECT_DIR`** — always resolve to the script's own directory.
4. **`LOG_FILE`** — always `${PROJECT_DIR}/launch.log`. Append, never truncate.
5. **`PORT`** — set even if unused (use `0` for non-network projects). MegaMan reads this.
6. **`exec`** — the final command **must** use `exec` so the process replaces the shell (MegaMan tracks the PID).
7. **Exit codes**: `0` = clean shutdown, non-zero = error.
8. **No backgrounding** — do not use `&` or `nohup`. MegaMan handles that via tmux.
9. **No hardcoded paths** — use `$PROJECT_DIR` for everything relative.

### Health Check (web services only)
If PORT > 0, MegaMan expects a health endpoint at:
```
GET http://localhost:${PORT}/health
```
Return HTTP 200 with any body. Add this to your app early in development.

## What MegaMan Does
- `run <project>` — executes `launch.sh` in a monitored tmux session
- `halt <project>` — stops the running process
- `jobs` — lists all running project jobs with status
- `output <project>` — shows recent log output

## What Claude Code Should Do
- Implement the project logic
- Fill in the `exec ...` line in `launch.sh` with the real start command
- Add a `/health` endpoint if this is a web service
- Keep `launch.log` in `.gitignore`
- If sudo is required:
  - Use the `$SUDO` variable in your exec command
  - Configure sudo NOPASSWD for the specific commands needed
  - Example: `exec $SUDO python -m uvicorn main:app --port 80`
  - Add a sudoers entry: `username ALL=(ALL) NOPASSWD: /usr/bin/python3, /usr/bin/node`
