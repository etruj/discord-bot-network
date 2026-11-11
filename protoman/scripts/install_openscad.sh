#!/bin/sh
# Install a recent OpenSCAD snapshot (Linux x86_64). Protoman needs the manifold
# backend, which the 2021.01 build in most distro repositories doesn't have.
#
#   scripts/install_openscad.sh [install dir] [link dir]
#
# Needs curl. The snapshot bundles most libraries but expects HarfBuzz from the
# system (package libharfbuzz0b on Debian/Ubuntu, harfbuzz on Fedora).
#
# Defaults install for your user: ~/.local/openscad, linked from ~/.local/bin.
# Set OPENSCAD_VERSION=2026.08.09 to pick a build; the default is the newest.
set -eu

DIR="${1:-$HOME/.local/openscad}"
BIN="${2:-$HOME/.local/bin}"
BASE="https://files.openscad.org/snapshots"
ARCH="$(uname -m)"
[ "$ARCH" = "x86_64" ] || { echo "No snapshot for $ARCH here; see https://openscad.org/downloads.html" >&2; exit 1; }

VER="${OPENSCAD_VERSION:-}"
if [ -z "$VER" ]; then
    VER="$(curl -fsSL "$BASE/" | grep -oE 'OpenSCAD-[0-9]{4}\.[0-9]{2}\.[0-9]{2}-x86_64\.AppImage' \
           | sort -u | tail -1 | sed -E 's/OpenSCAD-(.*)-x86_64.*/\1/')"
fi
[ -n "$VER" ] || { echo "Couldn't find a snapshot at $BASE" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading OpenSCAD $VER..."
curl -fsSL -o "$TMP/openscad.AppImage" "$BASE/OpenSCAD-$VER-x86_64.AppImage"
chmod +x "$TMP/openscad.AppImage"

# Extracted rather than run as an AppImage, so it needs no FUSE (containers).
(cd "$TMP" && ./openscad.AppImage --appimage-extract >/dev/null)
rm -rf "$DIR"
mkdir -p "$(dirname "$DIR")" "$BIN"
mv "$TMP/squashfs-root" "$DIR"
chmod -R a+rX "$DIR"          # extracted private to root otherwise
ln -sf "$DIR/usr/bin/openscad" "$BIN/openscad"
echo "Installed: $BIN/openscad"
