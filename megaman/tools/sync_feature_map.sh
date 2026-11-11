#!/usr/bin/env bash
# Copy ProtoMan's feature-map drawing code into the OpenSCAD skill.
#
# The labelled engineering drawing (E# edges, C# corners, H# holes, balloons,
# geometry table) lives in ProtoMan's engine. The OpenSCAD skill
# (scaffolds/3d-modeling/printable-parametric-parts) ships the SAME code, so
# every OpenSCAD project draws at the same quality. Source is the bot's tested
# snapshot (~/ProtoBot/engine/protobot), not the engine's dev folder.
#
# Files are copied unmodified; scripts/feature_map.py adapts them (STL → mask,
# notes line). Run after ProtoMan re-syncs his engine, then:
#   python3 scaffolds/3d-modeling/printable-parametric-parts/scripts/feature_map.py \
#       ~/ProtoBot/tests/fixtures/... (or any STL) /tmp/out
# New projects get the copy at /new; existing projects keep theirs.
set -euo pipefail
SRC="${1:-$HOME/ProtoBot/engine/protobot}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/scaffolds/3d-modeling/printable-parametric-parts/scripts/feature_map_engine"
FILES=(common.py refine.py drawing.py fasteners.py)
for f in "${FILES[@]}"; do [ -f "$SRC/$f" ] || { echo "missing $SRC/$f" >&2; exit 1; }; done
mkdir -p "$DEST"
for f in "${FILES[@]}"; do cp "$SRC/$f" "$DEST/$f"; done
cat > "$DEST/__init__.py" <<'PY'
"""Vendored from ProtoMan's engine (see VERSION). Do not edit here: change
the engine, let ProtoMan re-sync, then run claude-pm/tools/sync_feature_map.sh.
scripts/feature_map.py is the entry point."""
PY
{
  echo "copied $(date '+%Y-%m-%d %H:%M') from $SRC"
  head -2 "$SRC/../VERSION" 2>/dev/null | sed 's/^/protoman engine: /' || true
} > "$DEST/VERSION"
echo "feature_map_engine updated:"; cat "$DEST/VERSION"
