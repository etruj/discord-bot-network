#!/bin/bash
# One-time, run by the owner:   sudo bash install-sudo-gate.sh
#
# Replaces the blanket `NOPASSWD: ALL` grant with the claude-pm sudo gate:
#   - the claude-pm session (this bot's /debug session) keeps full root,
#   - every other session/job may run only scripts the owner approved via /sudo,
#   - the bot keeps exactly reboot / cancel-reboot / Claude Code update.
# The owner's own terminal sudo keeps working, with his password (wheel).
# Re-run after changing templates/cpm-sudo to reinstall the gate.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
OWNER="${SUDO_USER:?run with sudo from your own account, not a root shell}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
PROJECTS_ROOT="$OWNER_HOME/ai-projects"

install -d -m 755 -o root -g root /etc/cpm-sudo
install -m 755 -o root -g root "$HERE/templates/cpm-sudo" /usr/local/sbin/cpm-sudo
[ -f /etc/cpm-sudo/approved.json ] || { echo '{}' > /etc/cpm-sudo/approved.json; chmod 644 /etc/cpm-sudo/approved.json; }
cat > /etc/cpm-sudo/policy.json <<JSON
{"privileged_scopes": ["claude-cp-claude-pm.scope"], "projects_root": "$PROJECTS_ROOT"}
JSON
chmod 644 /etc/cpm-sudo/policy.json

# Must match SUDOERS_RULES in claude_pm.py (a unit test checks).
TMP="$(mktemp)"
cat > "$TMP" <<'SUDOERS'
# Managed by claude-pm (install-sudo-gate.sh). Sessions reach root only via
# the gate; the bot only for reboot and Claude Code updates.
__OWNER__ ALL=(root) NOPASSWD: /usr/local/sbin/cpm-sudo
__OWNER__ ALL=(root) NOPASSWD: /usr/bin/shutdown -r +1, /usr/bin/shutdown -c
__OWNER__ ALL=(root) NOPASSWD: /usr/bin/npm ^install -g @anthropic-ai/claude-code@[0-9][0-9.]*$
SUDOERS
sed -i "s/__OWNER__/$OWNER/" "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g root "$TMP" /etc/sudoers.d/claude-pm
rm -f "$TMP"
visudo -c >/dev/null

# An older blanket grant (NOPASSWD:ALL) would defeat the gate: list any so you can remove them.
grep -ls 'NOPASSWD: *ALL' /etc/sudoers.d/* 2>/dev/null | grep -v claude-pm \
    && echo "^ blanket NOPASSWD:ALL grants above give every Claude session root; remove them." || true
echo
echo "Passwordless rules for $OWNER now:"
sudo -l -U "$OWNER" | sed -n '/may run/,$p'
echo
echo "Next: /revive claude-pm (Discord) so that session picks up the gated sudo."
