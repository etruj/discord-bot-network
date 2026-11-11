#!/bin/sh
# Install Protoman as a systemd user service, run from this folder.
# Uses .venv/bin/python if there is one, otherwise python3.
set -eu

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
UNIT="$HOME/.config/systemd/user"

mkdir -p "$UNIT"
sed -e "s|@DIR@|$DIR|g" -e "s|@PYTHON@|$PY|g" "$DIR/deploy/protoman.service" > "$UNIT/protoman.service"
systemctl --user daemon-reload
systemctl --user enable --now protoman
echo "Running. Logs: journalctl --user -u protoman -f"
echo "To keep it running after you log out: loginctl enable-linger $USER"
