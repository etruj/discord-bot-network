#!/usr/bin/env bash
# Export one part module from a .scad file to a BINARY STL, then validate it.
#
# Usage:
#   ./export_and_validate.sh model.scad ModuleName out.stl
#
# Example:
#   ./export_and_validate.sh example.scad front_part front.stl
#
# Always exports binary (--export-format binstl). Uses xvfb-run automatically
# when there is no display (headless/CI).
#
# Resolution: $fa/$fs/$fn are dynamically scoped, so file-scope values in
# model.scad do NOT cross the `use <>` wrapper below — set them inside the part
# module. As a floor, the wrapper sets $fa=2 $fs=0.4 (override with PP_FA /
# PP_FS) instead of letting OpenSCAD's coarse 12/2 defaults through.
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "usage: $0 model.scad ModuleName out.stl" >&2
    exit 2
fi

SCAD="$1"; MOD="$2"; OUT="$3"
DIR="$(cd "$(dirname "$0")" && pwd)"

TMP="$(mktemp --suffix=.scad)"
trap 'rm -f "$TMP"' EXIT
printf 'use <%s>\n$fa = %s;\n$fs = %s;\n%s();\n' \
    "$(realpath "$SCAD")" "${PP_FA:-2}" "${PP_FS:-0.4}" "$MOD" > "$TMP"

RUN="openscad"
if [ -z "${DISPLAY:-}" ] && command -v xvfb-run >/dev/null 2>&1; then
    RUN="xvfb-run -a openscad"
fi

echo ">> exporting $MOD -> $OUT (binary STL)"
$RUN --export-format binstl -o "$OUT" "$TMP" 2>&1 | grep -E "Simple|Volumes|WARNING|ERROR" || true

echo ">> validating"
python3 "$DIR/validate_stl.py" "$OUT"
