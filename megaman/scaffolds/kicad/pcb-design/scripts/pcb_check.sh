#!/usr/bin/env bash
# ERC + DRC + board render — the PCB project's test gate.
# Usage: ./scripts/pcb_check.sh [project-basename]
# Exits non-zero on any ERC/DRC violation; artifacts land in build/.
set -e
cd "$(dirname "$0")/.."

if ! command -v kicad-cli >/dev/null; then
  echo "ERROR: kicad-cli not found — run install-kicad.sh (in the claude-pm repo)" >&2
  exit 1
fi

NAME="${1:-$(basename "$(ls -1 *.kicad_pro 2>/dev/null | head -1)" .kicad_pro)}"
if [ -z "$NAME" ]; then
  echo "ERROR: no .kicad_pro found and no name given" >&2
  exit 1
fi
mkdir -p build

if [ -f "$NAME.kicad_sch" ]; then
  kicad-cli sch erc --severity-error --exit-code-violations \
    -o build/erc.rpt "$NAME.kicad_sch"
  echo "ERC OK"
else
  echo "(no $NAME.kicad_sch — skipping ERC)"
fi

if [ -f "$NAME.kicad_pcb" ]; then
  kicad-cli pcb drc --severity-error --exit-code-violations \
    -o build/drc.rpt "$NAME.kicad_pcb"
  echo "DRC OK"
  kicad-cli pcb render -o build/board.png "$NAME.kicad_pcb"
  echo "Rendered build/board.png"
else
  echo "(no $NAME.kicad_pcb — skipping DRC/render)"
fi
