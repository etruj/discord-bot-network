#!/usr/bin/env bash
# Check prerequisites and install Python dependencies for all three bots.
# Safe to re-run. Installs nothing system-wide; it only tells you what is missing.
set -u
cd "$(dirname "$0")/.."
ok()   { printf '  ok       %s\n' "$1"; }
miss() { printf '  MISSING  %s  (%s)\n' "$1" "$2"; }

echo "Prerequisites"
command -v python3  >/dev/null && ok python3  || miss python3 "needed by all bots"
python3 -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null && ok "python >= 3.10" || miss "python >= 3.10" "upgrade python"
command -v openscad >/dev/null && ok openscad || miss openscad "Gutsman, Protoman: see protoman/scripts/install_openscad.sh"
command -v xvfb-run >/dev/null && ok xvfb     || miss xvfb "Gutsman renders: dnf/apt install xorg-x11-server-Xvfb / xvfb"
command -v admesh   >/dev/null && ok admesh   || miss admesh "mesh checks: dnf/apt install admesh"
command -v tmux     >/dev/null && ok tmux     || miss tmux "MegaMan only"
command -v claude   >/dev/null && ok claude   || miss "claude CLI" "MegaMan only: https://code.claude.com"

echo; echo "Python dependencies (per user, no sudo)"
for d in megaman protoman gutsman; do
  echo "  $d"; python3 -m pip install --user -q -r "$d/requirements.txt" && echo "    installed" || echo "    FAILED"
done

cat <<'MSG'

Next: docs/discord-setup.md (create the server, apps and channels), then run each
bot's setup. Start with Gutsman or Protoman; add MegaMan when you want sessions.
MSG
