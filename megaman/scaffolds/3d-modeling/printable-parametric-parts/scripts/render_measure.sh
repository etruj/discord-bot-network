#!/usr/bin/env bash
# Render a measurement-request view (a .scad that uses measure_lib.scad) to PNG.
#
# Usage:
#   ./render_measure.sh measure.scad out.png [ROT_X ROT_Z [DIST]]
#
# Defaults to a 3/4 view (62, 22). The same angles are passed to the model as
# $cam_rx / $cam_rz, so pp_mark_tag labels always face the camera. Render a
# second angle (e.g. 62 202 for the back) when a marker hides behind the part.
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: $0 measure.scad out.png [ROT_X ROT_Z [DIST]]" >&2
    exit 2
fi

SCAD="$1"; OUT="$2"; RX="${3:-62}"; RZ="${4:-22}"; DIST="${5:-300}"

RUN="openscad"
if [ -z "${DISPLAY:-}" ] && command -v xvfb-run >/dev/null 2>&1; then
    RUN="xvfb-run -a openscad"
fi

$RUN -o "$OUT" --camera=0,0,0,"$RX",0,"$RZ","$DIST" \
     --viewall --autocenter --imgsize=1400,1050 --colorscheme=Tomorrow \
     -D "\$cam_rx=$RX" -D "\$cam_rz=$RZ" "$SCAD"
echo ">> $OUT"
