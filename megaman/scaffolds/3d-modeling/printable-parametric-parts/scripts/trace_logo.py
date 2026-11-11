#!/usr/bin/env python3
"""Trace a raster logo (PNG/JPG) to a clean SVG for OpenSCAD import().

Pipeline: grayscale -> threshold -> crop tight -> 1-bit PBM -> potrace -> SVG.

Usage:
    python3 trace_logo.py in.png out.svg [--threshold 128] [--margin 8] [--invert]

--invert   : treat LIGHT pixels as the logo (light art on a dark background)

Then in OpenSCAD:
    resize([45,45]) import("out.svg", center=true);   // scale to 45 mm
Mirror in X if debossing on a face viewed from the back (the bed/outer face).
"""
import argparse
import subprocess
import sys

import numpy as np
from PIL import Image


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--threshold", type=int, default=128)
    ap.add_argument("--margin", type=int, default=8)
    ap.add_argument("--invert", action="store_true",
                    help="treat light pixels as foreground")
    a = ap.parse_args()

    im = Image.open(a.inp).convert("L")
    arr = np.array(im)
    mask = (arr > a.threshold) if a.invert else (arr < a.threshold)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        sys.exit("no foreground pixels found — adjust --threshold or try --invert")

    m = a.margin
    x0, x1 = max(0, xs.min() - m), min(arr.shape[1], xs.max() + m)
    y0, y1 = max(0, ys.min() - m), min(arr.shape[0], ys.max() + m)
    crop = im.crop((x0, y0, x1, y1))

    # potrace PBM wants black (0) = foreground
    if a.invert:
        bw = crop.point(lambda p: 0 if p > a.threshold else 255).convert("1")
    else:
        bw = crop.point(lambda p: 0 if p < a.threshold else 255).convert("1")

    pbm = a.out + ".pbm"
    bw.save(pbm)
    subprocess.run(["potrace", pbm, "-s", "--flat", "-o", a.out], check=True)
    print(f"wrote {a.out}  (logo bbox {x1 - x0} x {y1 - y0} px, square-ish recommended)")


if __name__ == "__main__":
    main()
