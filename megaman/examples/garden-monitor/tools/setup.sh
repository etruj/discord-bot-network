#!/usr/bin/env bash
# Root setup for garden-monitor.
#
# Sessions have NO general sudo. This script is how this project gets
# root, and only after the owner approves it by hash from Discord:
#
#   /sudo action:approve project:garden-monitor script:tools/setup.sh
#
# Then run it as:  sudo tools/setup.sh
# Editing this file cancels the approval — ask again after changes.
#
# Rules: keep it short, idempotent (safe to re-run), commented, and
# limited to the work that actually needs root. Everything else belongs
# in a normal script next to it in tools/.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as: sudo tools/setup.sh (needs /sudo approval first)" >&2
  exit 1
fi

# --- put root-only setup below (package installs, udev rules, mounts) ---
echo "nothing to do yet — add this project's root setup here"
