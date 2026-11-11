#!/usr/bin/env python3
"""Labelled engineering drawing (feature map) of an OpenSCAD part.

The same drawing ProtoMan posts for every part: Letter-landscape top view,
the part filled with a heavy outline, overall dimensions, the origin, and a
balloon on every feature (E# edges in blue boxes, C# corners in red circles,
H# holes in green circles), plus a table of the exact geometry of each:
"Y = 26.0;  X 10.7 → 59.2  (48.5 long)", "R10.0, center (…)",
"Ø6.0 thru;  center (…)". The user can then ask for changes by label
("E3 up 2", "C2 R5", "H1 Ø6") and you map the label to the .scad parameter.

It runs ProtoMan's own feature finder and drawing code (vendored in
feature_map_engine/, see its VERSION), so every project draws at the same
quality. This file only adapts the input: a 3D part is reduced to a 2D view,
rasterised finely, and handed to that code as if it were a traced sketch.

    python3 scripts/feature_map.py part.stl out/                 # top silhouette
    python3 scripts/feature_map.py part.scad out/ --module lid   # export first
    python3 scripts/feature_map.py part.stl out/ --z 2.5         # slice at Z=2.5
    python3 scripts/feature_map.py part.stl out/ --symmetry lr --version 3

Views:
  silhouette (default)  the outline seen from above + through-holes only
  --z H                 a cross-section at height H: shows blind pockets,
                        slots and bosses that the silhouette hides

Writes out/feature_map.png, out/feature_map.pdf and out/features.json
(every label with its kind, description and geometry, plus the offset
from the drawing's origin to the model's coordinates).

Needs: numpy, opencv-python, shapely, matplotlib, trimesh, Pillow
(and openscad on PATH for .scad input).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feature_map_engine import drawing, refine            # noqa: E402
from feature_map_engine.common import Trace                 # noqa: E402

PX_PER_MM = 20.0          # 0.05 mm per pixel: fine enough for 0.1 mm values
MAX_PX = 4000             # keep the mask a manageable size for big parts
MARGIN_PX = 40


# ── 3D → 2D ───────────────────────────────────────────────────────────────
def scad_to_stl(scad: Path, module: str | None, out: Path) -> Path:
    """Export a .scad (optionally one module of it) to a binary STL."""
    if not shutil.which("openscad"):
        raise SystemExit("openscad isn't on PATH; export the STL yourself")
    src = scad
    if module:
        src = out.parent / f"_fm_{module}.scad"
        src.write_text(f'use <{scad.resolve()}>\n{module}();\n')
    cmd = ["openscad", "--export-format", "binstl", "-o", str(out), str(src)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise SystemExit("openscad export failed:\n" + r.stderr[-2000:])
    return out


def part_outline(stl: Path, z: float | None):
    """(shapely Polygon in model mm, height, view text)."""
    import trimesh
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.ops import unary_union

    m = trimesh.load(str(stl), force="mesh")
    (x0, y0, z0), (x1, y1, z1) = m.bounds
    if z is None:
        poly = trimesh.path.polygons.projected(m, normal=[0, 0, 1], precise=True)
        view = "top silhouette (through-holes only)"
        if poly is None:
            raise SystemExit("couldn't project the mesh; is it empty?")
        # projected() returns the outline in its own plane frame; put it back
        # on the model's X/Y so the offset in features.json is right
        b = poly.bounds
        from shapely import affinity
        poly = affinity.translate(poly, x0 - b[0], y0 - b[1])
    else:
        if not z0 < z < z1:
            raise SystemExit(f"--z {z} is outside the part (Z {z0:.2f} → {z1:.2f})")
        sec = m.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
        if sec is None:
            raise SystemExit(f"nothing at Z = {z}")
        planar, to_3d = sec.to_planar(to_2D=np.eye(4))
        poly = unary_union(list(planar.polygons_full))
        view = f"section at Z = {z:g}"
    if isinstance(poly, MultiPolygon):
        parts = sorted(poly.geoms, key=lambda g: g.area, reverse=True)
        poly = parts[0]
        if len(parts) > 1:
            print(f"note: {len(parts) - 1} separate island(s) left out; "
                  "the drawing shows the largest body", file=sys.stderr)
    if not isinstance(poly, Polygon) or poly.area <= 0:
        raise SystemExit("the view came out empty")
    return poly, float(z1 - z0), view


def rasterise(poly, ppm: float, ss: int = 4):
    """Mask (uint8 0/255, image y down) of the polygon, without edge bias.

    fillPoly counts every pixel an edge touches as inside, which grows holes
    by about a pixel. Drawing at `ss`× and averaging down puts the boundary
    where half a pixel is covered, i.e. on the true edge. The solid is also
    grown half a pixel (mitred, so corners stay sharp): the engine traces the
    centres of the boundary pixels, which sit half a pixel inside the edge."""
    x0, y0, x1, y1 = poly.bounds
    poly = poly.buffer(0.5 / ppm, join_style=2, mitre_limit=10)
    w = int(np.ceil((x1 - x0) * ppm)) + 2 * MARGIN_PX
    h = int(np.ceil((y1 - y0) * ppm)) + 2 * MARGIN_PX
    big = np.zeros((h * ss, w * ss), np.uint8)
    k = ppm * ss

    def px(ring):
        a = np.asarray(ring.coords)
        # pixel centres sit at +0.5; scale about the pixel corner, not centre
        return np.stack([((a[:, 0] - x0) * ppm + MARGIN_PX) * ss - 0.5,
                         ((y1 - a[:, 1]) * ppm + MARGIN_PX) * ss - 0.5],
                        axis=1).round().astype(np.int32)
    cv2.fillPoly(big, [px(poly.exterior)], 255)
    if poly.interiors:
        cv2.fillPoly(big, [px(r) for r in poly.interiors], 0)
    small = cv2.resize(big, (w, h), interpolation=cv2.INTER_AREA)
    return np.where(small >= 128, 255, 0).astype(np.uint8)


def _trace(mask: np.ndarray, symmetry: str | None) -> Trace:
    """A Trace the engine accepts, built from the mask as is. The engine's own
    mask_to_polys / prepare redraw the shape (each pass grows holes ~1 px)
    and square up wobbly drawings; a modelled part is already exact."""
    cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    hier = hier[0]
    tops = [i for i in range(len(cnts)) if hier[i][3] == -1]
    main = max(tops, key=lambda i: cv2.contourArea(cnts[i]))
    outer = cnts[main].reshape(-1, 2).astype(float)
    holes = [cnts[i].reshape(-1, 2).astype(float) for i in range(len(cnts))
             if hier[i][3] == main]
    keep = np.zeros_like(mask)                       # the main body only
    cv2.drawContours(keep, [cnts[main]], -1, 255, cv2.FILLED)
    mask = np.where(keep > 0, mask, 0).astype(np.uint8)
    t = Trace(mask=mask, outer=outer, holes=holes, mode="model", work_image=255 - mask)
    t.symmetry = symmetry
    return t


# ── drawing ───────────────────────────────────────────────────────────────
def _replace_notes(note: str):
    """drawing.render writes a notes line meant for ProtoMan's extruded parts.
    Swap it for one that fits an OpenSCAD part, without editing the vendored
    file (it must stay identical to ProtoMan's so re-syncing is a copy)."""
    real_figure = drawing.plt.figure

    def figure(*a, **kw):
        fig = real_figure(*a, **kw)
        real_text = fig.text

        def text(x, y, s, *ta, **tkw):
            if isinstance(s, str) and s.startswith("All dims mm."):
                s = note
            return real_text(x, y, s, *ta, **tkw)
        fig.text = text
        return fig

    drawing.plt.figure = figure
    return lambda: setattr(drawing.plt, "figure", real_figure)


def feature_map(src: Path, out_dir: Path, *, name: str | None = None,
                module: str | None = None, z: float | None = None,
                symmetry: str | None = None, straighten: bool = True,
                version: int = 1, notes: str = "") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = name or module or src.stem
    with tempfile.TemporaryDirectory() as td:
        stl = src
        if src.suffix.lower() == ".scad":
            stl = scad_to_stl(src, module, Path(td) / "part.stl")
        poly, height, view = part_outline(stl, z)

    x0, y0, x1, y1 = poly.bounds
    L, W = round(x1 - x0, 2), round(y1 - y0, 2)
    height = round(height, 2)
    ox, oy = round(x0, 2) + 0.0, round(y0, 2) + 0.0      # +0.0: no "-0"
    ppm = min(PX_PER_MM, MAX_PX / max(L, W))
    t = _trace(rasterise(poly, ppm), symmetry)

    ys, xs = np.nonzero(t.mask)
    bx0, by1 = xs.min(), ys.max()

    def to_mm(pts):
        p = np.asarray(pts, float)
        return np.stack([(p[..., 0] - bx0) / ppm, (by1 - p[..., 1]) / ppm], axis=-1)

    sh = refine.Shape.from_trace(t, to_mm, False, L, W)
    ext, ints = sh.assemble(straighten)
    lo, hi = ext.min(axis=0), ext.max(axis=0)
    k = np.array([L, W]) / (hi - lo)

    def to_final(p):
        return (np.asarray(p, float) - lo) * k

    sh.remap(to_final, float(k.mean()), (float(L), float(W)))
    sh.length, sh.width = float(L), float(W)
    f_ext, f_ints = to_final(ext), [to_final(h) for h in ints]

    reqs = refine.requirements([], (L, W), ())
    for r in reqs:
        if r["kind"] == "size":
            r["show"] = float({"x": L, "y": W}[r["axis"]])
    note = (f"All dims mm, {view} of {src.name}. Origin = the part's bottom-left, "
            f"which is model X {ox:g}, Y {oy:g} (add that to a table value to get "
            f"the .scad coordinate). +X right, +Y up. Height {height:g} mm. "
            "Ask for changes by label, e.g. 'E3 up 2', 'E1 is 40', 'C2 R5', "
            "'H1 Ø6', 'H2 left 3'. " + notes)
    restore = _replace_notes(note)
    png, pdf = out_dir / "feature_map.png", out_dir / "feature_map.pdf"
    try:
        drawing.render(sh, f_ext, f_ints, str(png), str(pdf), name=name,
                       version=version, thickness=height,
                       requirements=list(dict.fromkeys(refine.req_desc(r) for r in reqs)),
                       locked=set(), prev=None, finish={})
    finally:
        restore()

    info = {"source": str(src), "view": view, "version": version,
            "size_mm": [L, W, height],
            "origin_in_model": [ox, oy],
            "symmetry": symmetry, "straighten": straighten,
            "features": sh.legend(), "warnings": sh.warnings,
            "png": str(png), "pdf": str(pdf)}
    (out_dir / "features.json").write_text(json.dumps(info, indent=2))
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("part", type=Path, help=".stl, or .scad (exported with openscad)")
    ap.add_argument("out_dir", type=Path, nargs="?", default=Path("drawings"))
    ap.add_argument("--module", help="with .scad: draw this module, e.g. lid")
    ap.add_argument("--name", help="title on the sheet (default: file or module name)")
    ap.add_argument("--z", type=float, help="cross-section height instead of the silhouette")
    ap.add_argument("--symmetry", choices=["lr", "tb", "both"],
                    help="label one side only; mirror partners become E#′")
    ap.add_argument("--raw", action="store_true",
                    help="don't snap straight edges / round holes (as modelled)")
    ap.add_argument("--version", type=int, default=1, help="v# in the title")
    ap.add_argument("--notes", default="", help="extra text for the notes line")
    a = ap.parse_args(argv)
    info = feature_map(a.part, a.out_dir, name=a.name, module=a.module, z=a.z,
                       symmetry=a.symmetry, straighten=not a.raw,
                       version=a.version, notes=a.notes)
    print(f"{info['png']}\n{info['pdf']}\n{a.out_dir / 'features.json'}")
    print(f"{len(info['features'])} features: "
          + ", ".join(f["label"] for f in info["features"]))
    for w in info["warnings"]:
        print("warning:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
