#!/usr/bin/env bash
# Install the newest OpenSCAD (with the fast Manifold backend) SYSTEM-WIDE.
#
# The newest OpenSCAD is only published as a dev-snapshot AppImage — Fedora's
# repo and Flathub are both stuck at 2021.01 — so "installing it properly" means
# extracting that AppImage into /opt and putting it on the global PATH. After
# this, `openscad` works for every user and every project, and re-running this
# script upgrades to the latest snapshot.
#
#   sudo bash scripts/install-openscad-global.sh
#   sudo openscad-update        # later, to pull the newest build
#
set -euo pipefail

PREFIX=/opt/openscad
LINK=/usr/local/bin/openscad
BASE=https://files.openscad.org/snapshots

if [ "$(id -u)" -ne 0 ]; then
    echo "This installs system-wide — run it as root:" >&2
    echo "  sudo bash $0" >&2
    exit 1
fi

echo ">> finding the latest OpenSCAD snapshot..."
FILE=$(curl -fsSL "$BASE/" \
    | grep -oE 'OpenSCAD-[0-9.]+-x86_64\.AppImage' | sort -V | uniq | tail -1)
[ -n "$FILE" ] || { echo "!! could not list snapshots at $BASE" >&2; exit 1; }
echo ">> latest is $FILE"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
echo ">> downloading..."
curl -fSL --progress-bar -o "$TMP/os.AppImage" "$BASE/$FILE"
chmod +x "$TMP/os.AppImage"

echo ">> extracting into $PREFIX ..."
( cd "$TMP" && ./os.AppImage --appimage-extract >/dev/null )   # no FUSE needed
rm -rf "$PREFIX"
mkdir -p "$(dirname "$PREFIX")"
mv "$TMP/squashfs-root" "$PREFIX"
chmod -R a+rX "$PREFIX"

ln -sf "$PREFIX/usr/bin/openscad" "$LINK"

# Desktop launcher + icon (best effort; harmless if paths differ)
ICON=$(find "$PREFIX" -maxdepth 2 -name 'openscad.png' 2>/dev/null | head -1 || true)
if [ -d /usr/share/applications ]; then
    cat > /usr/share/applications/openscad.desktop <<EOF
[Desktop Entry]
Type=Application
Name=OpenSCAD
Comment=The Programmers Solid 3D CAD Modeller
Exec=$LINK %F
Icon=${ICON:-openscad}
Categories=Graphics;3DGraphics;Engineering;
Terminal=false
EOF
fi

# Reusable updater: `sudo openscad-update` re-runs this installer.
install -m 0755 "$0" /usr/local/sbin/openscad-update 2>/dev/null || \
    cp "$0" /usr/local/sbin/openscad-update

echo
echo ">> installed : $("$LINK" --version 2>&1 | head -1)"
echo ">> command   : $(command -v openscad)  (should be $LINK)"
if "$LINK" --help 2>&1 | grep -qi 'backend'; then
    echo ">> Manifold  : available (fast renders)"
else
    echo ">> WARNING   : this build has no --backend option"
fi
echo ">> Note: Fedora's old openscad (2021.01) may still exist at /usr/bin/openscad;"
echo ">>       /usr/local/bin is ahead of it on PATH, so 'openscad' now uses the new one."
echo ">>       Remove the old package if you like:  sudo dnf remove openscad"
