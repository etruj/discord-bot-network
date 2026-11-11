#!/usr/bin/env bash
# setup.sh — verify (and help install) everything box-builder needs.
# Safe to re-run. Exits non-zero if a hard requirement is missing.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$DIR")"
fail=0
say() { printf '%-34s %s\n' "$1" "$2"; }

echo "=== box-builder setup check ==="

# --- OpenSCAD -------------------------------------------------------------
if command -v openscad >/dev/null 2>&1; then
    ver=$(openscad --version 2>&1 | head -1)
    if openscad --help 2>&1 | grep -qi -- '--backend'; then
        say "OpenSCAD" "OK  ($ver, Manifold — fast)"
    else
        say "OpenSCAD" "SLOW ($ver — no Manifold backend)"
        echo "    Renders will take minutes instead of seconds. Install a current build:"
        echo "      sudo bash $DIR/install-openscad-global.sh"
    fi
else
    say "OpenSCAD" "MISSING"
    echo "    Install it:  sudo bash $DIR/install-openscad-global.sh"
    fail=1
fi

# --- engine ---------------------------------------------------------------
LIB="$ROOT/tools/monoscad/rugged-box/rugged-box-library.scad"
if [ -f "$LIB" ]; then
    say "rugged-box engine" "OK  (tools/monoscad/rugged-box)"
else
    say "rugged-box engine" "MISSING — fetching..."
    if command -v git >/dev/null 2>&1; then
        rm -rf "$ROOT/tools/monoscad"
        if git clone --depth 1 -q https://github.com/smkent/monoscad \
                "$ROOT/tools/monoscad" 2>/dev/null && [ -f "$LIB" ]; then
            say "rugged-box engine" "OK  (cloned smkent/monoscad)"
        else
            say "rugged-box engine" "FAILED to clone"; fail=1
        fi
    else
        say "git" "MISSING (needed to fetch the engine)"; fail=1
    fi
fi

# --- Python deps ----------------------------------------------------------
for mod in trimesh numpy; do
    if python3 -c "import $mod" 2>/dev/null; then
        say "python: $mod" "OK"
    else
        say "python: $mod" "MISSING  ->  pip install $mod"
        fail=1
    fi
done

if command -v admesh >/dev/null 2>&1; then
    say "admesh" "OK"
else
    say "admesh" "MISSING (printability tests will be skipped)"
    echo "    Install:  sudo dnf install -y admesh   # or apt install admesh"
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "All good. Try:"
    echo "  python3 $DIR/box_wizard.py --json --width 160 --length 100 \\"
    echo "      --bottom-height 45 --top-height 18 --latch-count 2 --out ./out"
else
    echo "Some requirements are missing (see above)."
fi
exit "$fail"
