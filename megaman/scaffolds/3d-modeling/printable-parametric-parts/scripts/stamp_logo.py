#!/usr/bin/env python3
"""Stamp a logo (SVG) onto an existing STL model.

Uses OpenSCAD for the boolean (robust, handles SVG holes natively), so the only
deps are openscad + trimesh (both already required by this skill).

Examples:
    # deboss your logo on the bottom (visible back) face, 45 mm, 0.4 mm deep
    python3 stamp_logo.py back_plate.stl

    # top face, custom size/depth, explicit output
    python3 stamp_logo.py lid.stl -o lid_logo.stl --face top --size 30 --depth 0.5

    # emboss (raised) instead of recessed
    python3 stamp_logo.py panel.stl --mode emboss --face top

Default logo is assets/logo.svg next to this script. Override with --logo.

Notes
-----
* The default mirrors the art on bottom/-facing faces so it reads correctly to a
  viewer looking at that face. Override with --mirror yes|no.
* Embossing on a face that prints against the bed (usually 'bottom') can't be
  printed — the script warns. Recess (deboss) on the visible bed face instead.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import trimesh


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Stamp an SVG logo onto an STL.")
    ap.add_argument("model", help="input STL")
    ap.add_argument("-o", "--out", help="output STL (default: <model>_logo.stl)")
    ap.add_argument("--logo", default=os.path.normpath(os.path.join(here, "..", "assets", "logo.svg")),
                    help="logo SVG (default: bundled assets/logo.svg)")
    ap.add_argument("--face", choices=["bottom", "top"], default="bottom")
    ap.add_argument("--size", type=float, default=45.0, help="logo size in mm (max dim)")
    ap.add_argument("--depth", type=float, default=0.4, help="deboss/emboss depth mm")
    ap.add_argument("--mode", choices=["deboss", "emboss"], default="deboss")
    ap.add_argument("--at", default=None, help="X,Y position on the face (default: bbox center)")
    ap.add_argument("--mirror", choices=["auto", "yes", "no"], default="auto")
    args = ap.parse_args()

    if not shutil.which("openscad"):
        sys.exit("openscad not found on PATH")
    if not os.path.exists(args.logo):
        sys.exit(f"logo not found: {args.logo}")

    out = args.out or os.path.splitext(args.model)[0] + "_logo.stl"

    # bounding box for placement
    m = trimesh.load(args.model, force="mesh")
    (x0, y0, z0), (x1, y1, z1) = m.bounds
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    if args.at:
        cx, cy = (float(v) for v in args.at.split(","))

    mirror = (args.face == "bottom") if args.mirror == "auto" else (args.mirror == "yes")
    msign = -1 if mirror else 1
    eps = 0.05

    if args.mode == "emboss" and args.face == "bottom":
        print("WARNING: emboss on the bottom face cannot print against the bed; "
              "use --mode deboss or --face top.")

    # Build the cutter/adder z-extent
    if args.face == "bottom":
        if args.mode == "deboss":
            z_lo, z_hi = z0 - eps, z0 + args.depth          # cut up into the part
        else:
            z_lo, z_hi = z0 - args.depth, z0 + eps          # protrude below
    else:  # top
        if args.mode == "deboss":
            z_lo, z_hi = z1 - args.depth, z1 + eps          # cut down into the part
        else:
            z_lo, z_hi = z1 - eps, z1 + args.depth          # protrude above
    height = z_hi - z_lo

    op = "difference" if args.mode == "deboss" else "union"
    scad = f'''
$fn = 64;
{op}() {{
    import("{os.path.abspath(args.model)}", convexity=10);
    translate([{cx}, {cy}, {z_lo}])
        linear_extrude({height})
            scale([{msign}, 1, 1])
                resize([{args.size}, {args.size}])
                    import("{os.path.abspath(args.logo)}", center=true);
}}
'''
    with tempfile.NamedTemporaryFile("w", suffix=".scad", delete=False) as f:
        f.write(scad)
        tmp = f.name

    run = ["openscad", "--export-format", "binstl", "-o", out, tmp]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        run = ["xvfb-run", "-a"] + run
    print(f">> stamping {args.logo} onto {args.model} ({args.mode}, {args.face}) -> {out}")
    res = subprocess.run(run, capture_output=True, text=True)
    os.unlink(tmp)
    for line in (res.stdout + res.stderr).splitlines():
        if any(k in line for k in ("Simple", "Volumes", "WARNING", "ERROR")):
            print("   " + line.strip())
    if res.returncode != 0:
        sys.exit("openscad failed")

    # validate if the validator is present
    vp = os.path.join(here, "validate_stl.py")
    if os.path.exists(vp):
        subprocess.run([sys.executable, vp, out])
    print(f"done: {out}")


if __name__ == "__main__":
    main()
