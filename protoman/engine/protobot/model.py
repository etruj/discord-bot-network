"""Traced silhouette + basic measurements -> parametric OpenSCAD model.

Measurements:
  length    X extent of the part, mm
  width     Y extent of the part, mm
  thickness Z extrusion, mm

The outline is stored in the .scad as unit-box coordinates (0..1 on both
axes) and scaled by LENGTH/WIDTH there, so the generated file stays fully
parametric: edit LENGTH/WIDTH/THICKNESS at the top and re-export.

If the sketch's own proportions don't match length:width, the shape is
stretched to fit (that's the point — the sketch gives the shape, the numbers
give the size) and a warning reports by how much.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import cv2
import numpy as np

from .common import Trace

# --- print target: Prusa Core One, 0.4 mm nozzle, PLA ----------------------
NOZZLE = 0.4
MIN_FEATURE = 0.8          # mm, smallest wall/feature that prints reliably
LAYER_H = 0.2              # mm, chosen layer height
VERSION_DEPTH = 0.4        # mm, = 2 layers at 0.2
VERSION_TEXT_MIN = 3.0     # mm, smaller text strokes drop below nozzle width
VERSION_TEXT_MAX = 6.0
# Stretch beyond this (ratio of sketch aspect to requested aspect) gets a warning.
STRETCH_WARN = 1.15


@dataclass
class Model:
    length: float
    width: float
    thickness: float
    outer: np.ndarray                 # (N,2) unit-box coords, y up
    holes: list[np.ndarray]
    rotated: bool                     # sketch was turned 90° to fit L/W
    stretch: float                    # >1: non-uniform scale factor applied
    version: int = 1
    version_pos: tuple[float, float] | None = None   # unit-box coords
    version_size: float = 0.0
    finish: dict = field(default_factory=lambda: {"TOP": ("none", 0.0),
                                                  "BOTTOM": ("none", 0.0)})
    warnings: list[str] = field(default_factory=list)
    # screw recesses / nut pockets: [{"label","m","x","y" (mm), "cuts": [(shape,size,depth,face)]}]
    fasteners: list = field(default_factory=list)

    def mm(self, pts: np.ndarray) -> np.ndarray:
        return pts * [self.length, self.width]


def _normalise(pts, xmin, ymin, w, h, rotate):
    u = (pts[:, 0] - xmin) / w
    v = (ymin + h - pts[:, 1]) / h          # image y-down -> model y-up
    if rotate:                               # -90° turn, kept in the unit box
        u, v = v, 1 - u
    return np.column_stack([u, v])


def _raster_mm(m: Model, res: float, bottom_cuts: bool = False):
    """Rasterise the part's footprint at `res` mm/px (with a margin).
    bottom_cuts=True also clears the recesses/pockets cut into the bottom
    face (where the version stamp goes)."""
    pad = 4
    W = int(np.ceil(m.length / res)) + 2 * pad
    H = int(np.ceil(m.width / res)) + 2 * pad
    img = np.zeros((H, W), np.uint8)

    def px(p):
        q = m.mm(p) / res + pad
        q[:, 1] = H - q[:, 1]
        return np.round(q).astype(np.int32)

    cv2.fillPoly(img, [px(m.outer)], 255)
    for h in m.holes:
        cv2.fillPoly(img, [px(h)], 0)
    if bottom_cuts:
        for f in m.fasteners:
            for shape, size, _, face in f["cuts"]:
                if face == "bottom":
                    r = size / 2 / (np.cos(np.radians(30)) if shape == "hex" else 1)
                    c = px(np.array([[f["x"] / m.length, f["y"] / m.width]]))[0]
                    cv2.circle(img, (int(c[0]), int(c[1])), int(np.ceil(r / res)) + 1, 0, -1)
    return img, pad, H


def _check_and_place(m: Model) -> None:
    """Thin-feature check + find a spot for the version stamp."""
    res = max(0.05, max(m.length, m.width) / 3000)
    img, pad, H = _raster_mm(m, res)

    # Features thinner than MIN_FEATURE vanish under an opening of that size.
    k = max(3, int(round(MIN_FEATURE / res)) | 1)
    opened = cv2.morphologyEx(img, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    lost = np.count_nonzero(img & ~opened) * res * res
    total = np.count_nonzero(img) * res * res
    if total and lost / total > 0.01:
        m.warnings.append(f"some parts of the shape are thinner than "
                          f"{MIN_FEATURE} mm (~{lost:.0f} mm² of {total:.0f} mm²) "
                          f"and may not print — thicken those areas in the sketch "
                          f"or scale the part up")

    place_version(m)


def _warn_once(m: Model, msg: str) -> None:
    if msg not in m.warnings:
        m.warnings.append(msg)


def place_version(m: Model) -> None:
    """Version text goes at the point deepest inside the bottom face."""
    m.version_pos, m.version_size = None, 0.0
    if m.thickness < VERSION_DEPTH + 2 * LAYER_H:
        _warn_once(m, "too thin for a version stamp; skipped")
        return
    res = max(0.05, max(m.length, m.width) / 3000)
    img, pad, H = _raster_mm(m, res, bottom_cuts=True)
    dt = cv2.distanceTransform(img, cv2.DIST_L2, 5)
    _, r, _, (cx, cy) = cv2.minMaxLoc(dt)
    r_mm = r * res
    # "v12" is ~1.9x its height wide: half-diagonal ~1.07x the height.
    # Keep the whole text box inside the clear circle, with a 15% margin.
    size = min(VERSION_TEXT_MAX, r_mm / 1.25)
    if size < VERSION_TEXT_MIN:
        _warn_once(m, "no area large enough for a version stamp; skipped")
        return
    x_mm = (cx - pad) * res
    y_mm = (H - cy - pad) * res
    m.version_pos = (x_mm / m.length, y_mm / m.width)
    m.version_size = round(size, 1)


def build_model(t: Trace, length: float, width: float, thickness: float,
                orient: str = "auto", version: int = 1, refiner=None,
                finish: dict | None = None) -> Model:
    """Map a trace onto the requested length x width box.

    orient "auto"     turn the sketch 90° if that makes its long side match
                      the longer of length/width (less stretching)
           "as-drawn" length always runs along the image's horizontal
    refiner           optional f(to_mm, rotated) -> (outer_mm, holes_mm, keep):
                      reworks the outline in real millimetres (refine.py —
                      symmetry, straightening, label edits). With keep=True
                      the result is re-fitted to exactly length x width;
                      keep=False (an edit moved an edge) takes its new size.
    """
    for name, v in (("length", length), ("width", width), ("thickness", thickness)):
        if not v or v <= 0:
            raise ValueError(f"{name} must be a positive number of mm, got {v!r}")
        if v > 1000:
            raise ValueError(f"{name} {v} mm is larger than any printer bed")
    if orient not in ("auto", "as-drawn"):
        raise ValueError(f"orient must be auto/as-drawn, not {orient!r}")

    xmin, ymin, xmax, ymax = t.bbox
    w, h = xmax - xmin, ymax - ymin
    if w < 5 or h < 5:
        raise ValueError("the traced shape is only a few pixels — draw it larger")
    rotate = orient == "auto" and abs(length - width) > 1e-6 \
        and (w > h) != (length > width) and abs(w - h) / max(w, h) > 0.05
    sw, sh = (h, w) if rotate else (w, h)
    stretch = (length / width) / (sw / sh)
    stretch = max(stretch, 1 / stretch)

    if refiner is None:
        outer = _normalise(t.outer, xmin, ymin, w, h, rotate)
        holes = [_normalise(hp, xmin, ymin, w, h, rotate) for hp in t.holes]
    else:
        def to_mm(pts):
            return _normalise(np.asarray(pts, float), xmin, ymin, w, h, rotate) \
                * [length, width]
        outer_mm, holes_mm, keep_size = refiner(to_mm, rotate)
        lo, hi = outer_mm.min(axis=0), outer_mm.max(axis=0)
        outer = (outer_mm - lo) / (hi - lo)
        holes = [(hp - lo) / (hi - lo) for hp in holes_mm]
        if not keep_size:             # a label edit moved an outside edge
            nl, nw = (round(float(v), 2) for v in hi - lo)
            # sub-0.05 mm differences are rounding noise, not a real move
            length = length if abs(nl - length) < 0.05 else nl
            width = width if abs(nw - width) < 0.05 else nw
    m = Model(
        length=float(length), width=float(width), thickness=float(thickness),
        outer=outer, holes=holes,
        rotated=rotate, stretch=float(stretch), version=version,
    )
    if rotate:
        m.warnings.append("sketch turned 90° so its long side matches the "
                          "longer measurement (use orient=as-drawn to stop this)")
    if stretch > STRETCH_WARN:
        m.warnings.append(f"the traced shape's proportions differ from {length}x{width}; "
                          f"it was stretched {stretch:.2f}x along one axis to fit")
    layers = thickness / LAYER_H
    if abs(layers - round(layers)) > 1e-6:
        m.warnings.append(f"thickness {thickness} mm isn't a whole number of "
                          f"{LAYER_H} mm layers; the slicer will round it to "
                          f"{round(layers) * LAYER_H:.2f} mm")
    if thickness < MIN_FEATURE:
        m.warnings.append(f"thickness {thickness} mm is below the {MIN_FEATURE} mm "
                          f"minimum and will be fragile")
    if finish:
        _set_finish(m, finish)
    _check_and_place(m)
    return m


def _set_finish(m: Model, finish: dict) -> None:
    """Top/bottom perimeter edge finish: ('fillet'|'chamfer'|'none', size)."""
    for side in ("TOP", "BOTTOM"):
        kind, r = finish.get(side, ("none", 0.0))
        r = float(r or 0)
        if kind != "none" and r <= 0:
            kind = "none"
        m.finish[side] = (kind, r)
    top, bot = m.finish["TOP"][1], m.finish["BOTTOM"][1]
    layers = lambda r: np.ceil(r / LAYER_H - 1e-6) * LAYER_H
    if layers(top) + layers(bot) > m.thickness - LAYER_H + 1e-6:
        raise ValueError(f"edge finish too big for a {m.thickness:g} mm thick part: "
                         f"top {top:g} + bottom {bot:g} must leave at least one "
                         f"{LAYER_H} mm layer — make them smaller")
    if m.finish["BOTTOM"][0] == "fillet" and bot > 0.6:
        m.warnings.append(f"a bottom R{bot:g} overhangs right at the bed edge and can "
                          f"curl; 'bottom edges chamfer {bot:g}' prints cleaner")
    # anything thinner than 2x the finish gets eaten by it
    big = max(top, bot)
    if big > 0:
        res = max(0.05, max(m.length, m.width) / 3000)
        img, _, _ = _raster_mm(m, res)
        k = int(round(2 * big / res)) | 1
        eroded = cv2.erode(img, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        n0 = cv2.connectedComponents(img)[0]
        n1 = cv2.connectedComponents(eroded)[0]
        if n1 != n0 or np.count_nonzero(eroded) == 0:
            raise ValueError(f"an edge finish of {big:g} mm would eat through thin parts "
                             f"of the shape (narrower than {2 * big:g} mm) — use a "
                             f"smaller size")
        for side in ("TOP", "BOTTOM"):
            kind, r = m.finish[side]
            if kind != "none" and abs(r / LAYER_H - round(r / LAYER_H)) > 1e-6:
                m.warnings.append(f"{side.lower()} edge {kind} {r:g} is built in "
                                  f"{LAYER_H} mm steps (layer height), so it reaches "
                                  f"{layers(r):.1f} mm")


def _fmt_pts(pts: np.ndarray, per_line: int = 6) -> str:
    items = [f"[{x:.5f},{y:.5f}]" for x, y in pts]
    lines = [", ".join(items[i:i + per_line]) for i in range(0, len(items), per_line)]
    return ",\n    ".join(lines)


def _fmt_fasteners(m: Model) -> str:
    if not m.fasteners:
        return "[]"
    rows = []
    for f in m.fasteners:
        cuts = ", ".join(f'["{sh}", {sz:g}, {d:g}, "{fc}"]' for sh, sz, d, fc in f["cuts"])
        rows.append((f"    [[{f['x'] / m.length:.5f}, {f['y'] / m.width:.5f}], [{cuts}]]",
                     f"   // {f['label']} {f['m']}"))
    return "[\n" + "\n".join(r + ("," if i < len(rows) - 1 else "") + c
                               for i, (r, c) in enumerate(rows)) + "\n]"


def to_scad(m: Model, name: str, source: str = "") -> str:
    """Render the model as a self-contained, parametric .scad file."""
    pts = [m.outer, *m.holes]
    offsets = np.cumsum([0] + [len(p) for p in pts])
    paths = [list(range(offsets[i], offsets[i + 1])) for i in range(len(pts))]
    all_pts = np.vstack(pts)
    # polygon() needs real index lists — a bare range [a:b] is rejected
    # and the paths silently ignored (outline + holes fuse into one path).
    paths_txt = ",\n    ".join(f"[for (i = [{p[0]}:{p[-1]}]) i]" for p in paths)
    vpos = (f"[{m.version_pos[0]:.4f}, {m.version_pos[1]:.4f}]"
            if m.version_pos else "undef")
    return f"""\
// {name} — generated by protobot from a sketch{f' ({source})' if source else ''}
// {date.today().isoformat()}
//
// Print: Prusa Core One, 0.4 mm nozzle, PLA, {LAYER_H} mm layers, flat on the bed.
// The outline is stored in a 0..1 unit box and scaled by LENGTH/WIDTH, so
// the three measurements below are all you need to change. Re-export with:
//   openscad --backend=manifold --export-format binstl -o {name}.stl {name}.scad
//
// Dimension tags: [M] = given by the user, [S] = traced from the sketch.

/* [Measurements] */
LENGTH    = {m.length:g};   // [M] X extent, mm
WIDTH     = {m.width:g};   // [M] Y extent, mm
THICKNESS = {m.thickness:g};   // [M] extrusion height, mm

/* [Edge finish] */
// Perimeter edges (outline AND holes), top and bottom faces.
// "fillet" = rounded, "chamfer" = 45°, "none" = sharp. Built in LAYER steps,
// i.e. exactly as the printer will lay it down.
EDGE_TOP_KIND    = "{m.finish['TOP'][0]}";
EDGE_TOP         = {m.finish['TOP'][1]:g};     // mm
EDGE_BOTTOM_KIND = "{m.finish['BOTTOM'][0]}";
EDGE_BOTTOM      = {m.finish['BOTTOM'][1]:g};     // mm
LAYER            = {LAYER_H};   // mm, print layer height

/* [Version stamp] */
VERSION        = {m.version};       // bump every revision
VERSION_POS    = {vpos};  // [S] unit-box spot deepest inside the shape
VERSION_SIZE   = {m.version_size:g};      // mm text height
VERSION_DEPTH  = {VERSION_DEPTH};    // mm, 2 layers at {LAYER_H}

/* [Screws and nuts] */
// One entry per screw hole: [x, y] in the unit box (scaled by LENGTH/WIDTH),
// then its cuts: ["circle", Ø] = screw-head recess, ["hex", across flats] =
// nut pocket, each [.., depth mm, "top"|"bottom"]. The clearance hole itself
// is part of the outline below.
FASTENERS = {_fmt_fasteners(m)};

/* [Hidden] */
// [S] traced outline (+ holes), unit box, y up.
OUTLINE_PTS = [
    {_fmt_pts(all_pts)}
];
OUTLINE_PATHS = [
    {paths_txt}
];

module outline_2d() {{
    scale([LENGTH, WIDTH]) polygon(points = OUTLINE_PTS, paths = OUTLINE_PATHS);
}}

// Debossed into the bottom face, mirrored so it reads from underneath.
module version_stamp() {{
    if (!is_undef(VERSION_POS) && VERSION_SIZE > 0)
        translate([VERSION_POS[0] * LENGTH, VERSION_POS[1] * WIDTH, -0.01])
            linear_extrude(VERSION_DEPTH + 0.01)
                mirror([1, 0])
                    text(str("v", VERSION), size = VERSION_SIZE,
                         font = "Liberation Sans:style=Bold",
                         halign = "center", valign = "center");
}}

// Screw-head recesses and nut pockets, cut into the top or bottom face.
module fastener_cuts() {{
    for (f = FASTENERS) for (c = f[1])
        translate([f[0][0] * LENGTH, f[0][1] * WIDTH,
                   c[3] == "top" ? THICKNESS - c[2] : -0.01])
            linear_extrude(c[2] + 0.01 + (c[3] == "top" ? 0.01 : 0))
                if (c[0] == "hex") circle(d = c[1] / cos(30), $fn = 6);
                else circle(d = c[1]);
}}

// How far the outline is pulled in at depth d (0..r) into a finished edge.
function edge_inset(kind, r, d) =
    kind == "chamfer" ? max(0, r - d) :
    kind == "fillet"  ? r - sqrt(max(0, r * r - (r - min(d, r)) * (r - min(d, r)))) : 0;

module body() {{
    nb = EDGE_BOTTOM_KIND == "none" ? 0 : ceil(EDGE_BOTTOM / LAYER - 1e-6);
    nt = EDGE_TOP_KIND    == "none" ? 0 : ceil(EDGE_TOP / LAYER - 1e-6);
    zb = nb * LAYER;
    zt = nt * LAYER;
    union() {{
        // straight-walled core, overlapping the finished layers (no shared faces)
        translate([0, 0, zb - (nb > 0 ? 0.01 : 0)])
            linear_extrude(THICKNESS - zb - zt + (nb > 0 ? 0.01 : 0) + (nt > 0 ? 0.01 : 0))
                outline_2d();
        if (nb > 0) for (i = [0 : nb - 1])
            translate([0, 0, i * LAYER])
                linear_extrude(LAYER + 0.01)
                    offset(delta = -edge_inset(EDGE_BOTTOM_KIND, EDGE_BOTTOM, (i + 0.5) * LAYER))
                        outline_2d();
        if (nt > 0) for (i = [0 : nt - 1])
            translate([0, 0, THICKNESS - (i + 1) * LAYER - (i < nt - 1 ? 0.01 : 0)])
                linear_extrude(LAYER + (i < nt - 1 ? 0.01 : 0))
                    offset(delta = -edge_inset(EDGE_TOP_KIND, EDGE_TOP, (i + 0.5) * LAYER))
                        outline_2d();
    }}
}}

module part() {{
    $fa = 2; $fs = 0.4;
    difference() {{
        body();
        version_stamp();
        fastener_cuts();
    }}
}}

part();
"""
