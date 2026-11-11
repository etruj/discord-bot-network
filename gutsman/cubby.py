#!/usr/bin/env python3
"""cubby.py — the shared divided-tray (cubby) insert primitive.

Both entry points that make a cubby tray — the inline option in `/rugged` and
the standalone `/inserts` command — call through here, so the geometry lives in
exactly one place (`scad/cubby_insert.scad`) and both stay in lockstep. This is
a shared *primitive*, not a shared questionnaire: each wizard still owns its own
conversation and just hands us numbers.

The tray is sized from a box *interior*. Given the interior width/length and a
per-side `clearance`, the tray outer size is `interior - 2*clearance`, and its
outer corner radius is the box's interior radius reduced by the same clearance
so it drops in with uniform play instead of binding on the corners.
"""
import math
from pathlib import Path

import render

SCAD = Path(__file__).resolve().parent / "scad" / "cubby_insert.scad"

# Per-side clearance presets (mm), mirroring the plan's fit ladder.
CLEARANCES = {"tight": 0.10, "standard": 0.15, "easy": 0.20, "loose": 0.30}

# Counts the equal-split UI offers, in the order boxes usually want them.
SPLIT_COUNTS = (2, 3, 4, 6, 8)

# Smallest compartment edge we'll call practical to print and use (mm).
MIN_CELL = 15.0

# Engineering defaults for the tray itself (not the box).
WALL = 2.0
FLOOR = 1.5
DIVIDER = 1.5
CHAMFER = 2.5


def arrange(n: int, w: float, l: float) -> tuple:
    """Split n compartments into the (cols, rows) grid closest to square cells.

    Tried against the interior aspect ratio: for each factor pair of n we score
    how square the resulting cells are and keep the best, orienting more columns
    along the longer axis. n need not be a perfect product — a prime like 3 just
    yields 3x1 / 1x3.
    """
    best, best_score = (n, 1), None
    for cols in range(1, n + 1):
        if n % cols:
            continue
        rows = n // cols
        cell_w = w / cols
        cell_l = l / rows
        score = max(cell_w, cell_l) / max(1e-6, min(cell_w, cell_l))
        if best_score is None or score < best_score:
            best, best_score = (cols, rows), score
    return best


def cell_size(cols: int, rows: int, w: float, l: float) -> tuple:
    """The (width, length) of one compartment for a given grid and interior."""
    return ((w - 2 * WALL - (cols - 1) * DIVIDER) / cols,
            (l - 2 * WALL - (rows - 1) * DIVIDER) / rows)


def cell_ok(cols: int, rows: int, w: float, l: float,
            min_cell: float = MIN_CELL) -> bool:
    """Whether every compartment edge clears `min_cell` (i.e. prints usable)."""
    if cols < 1 or rows < 1:
        return False
    cw, cl = cell_size(cols, rows, w, l)
    return cw >= min_cell and cl >= min_cell


def feasible_counts(w: float, l: float, min_cell: float = MIN_CELL) -> list:
    """Which SPLIT_COUNTS leave every compartment edge above `min_cell`.

    Keeps the wizard from offering layouts that would print as unusable slots.
    """
    return [n for n in SPLIT_COUNTS
            if cell_ok(*arrange(n, w, l), w, l, min_cell)]


def tray_outer(col_widths, row_lengths) -> tuple:
    """The tray's outer (width, length) from its cells, walls and dividers.

    This is what the box interior must clear: a box built around listed
    compartments derives its interior as this plus the fit clearance.
    """
    ow = sum(col_widths) + (len(col_widths) - 1) * DIVIDER + 2 * WALL
    ol = sum(row_lengths) + (len(row_lengths) - 1) * DIVIDER + 2 * WALL
    return ow, ol


def defines(col_widths, row_lengths, height: float,
            corner_radius: float) -> dict:
    """The -D map for scad/cubby_insert.scad. Cells are literal usable sizes.

    `corner_radius` is the tray's *own* outer radius — the caller has already
    reduced the box interior radius by the clearance where that matters.
    """
    return {
        "col_widths": [round(w, 3) for w in col_widths],
        "row_lengths": [round(l, 3) for l in row_lengths],
        "insert_height": round(height, 3),
        "corner_radius": round(max(0.0, corner_radius), 3),
        "divider_thickness": DIVIDER,
        "chamfer": CHAMFER,
        "wall_thickness": WALL,
        "floor_thickness": FLOOR,
    }


def build(col_widths, row_lengths, height: float, out_stl: Path,
          corner_radius: float = 0.0, timeout: int = 180):
    """Generate one cubby tray STL from explicit cells. Returns (ok, error)."""
    out_stl = Path(out_stl)
    out_stl.parent.mkdir(parents=True, exist_ok=True)
    d = defines(col_widths, row_lengths, height, corner_radius)
    return render.build_stl(SCAD, out_stl, d, timeout=timeout)


def fit_cells(inner_w: float, inner_l: float, col_widths, row_lengths,
              clearance: float) -> tuple:
    """Scale listed cells so the tray fills a box interior at an exact clearance.

    The box interior is rounded up to whole mm (the generator only takes integer
    dimensions), which would otherwise leave an uneven, often-loose gap around a
    tray built at literal sizes. Scaling the cells (dividers held fixed) makes
    the tray's outer size exactly `interior - 2*clearance` on both axes, so the
    tray-to-case fit is a consistent, snug `clearance` per side. Cells only ever
    grow — by the sub-millimetre rounding slack — never shrink below what was
    asked for.
    """
    span_w_cells = sum(col_widths)
    span_l_cells = sum(row_lengths)
    target_w = inner_w - 2 * clearance - 2 * WALL - (len(col_widths) - 1) * DIVIDER
    target_l = inner_l - 2 * clearance - 2 * WALL - (len(row_lengths) - 1) * DIVIDER
    fw = target_w / span_w_cells if span_w_cells else 1.0
    fl = target_l / span_l_cells if span_l_cells else 1.0
    return ([round(w * fw, 3) for w in col_widths],
            [round(l * fl, 3) for l in row_lengths])


def pocket_defines(pockets, span_w: float, span_l: float, height: float,
                   corner_radius: float) -> dict:
    """The -D map for an explicit ragged/nested layout (a list of [x,y,w,l])."""
    return {
        "pockets": [[round(v, 3) for v in p] for p in pockets],
        "inner_w": round(span_w + 2 * WALL, 3),
        "inner_l": round(span_l + 2 * WALL, 3),
        "insert_height": round(height, 3),
        "corner_radius": round(max(0.0, corner_radius), 3),
        "chamfer": CHAMFER,
        "wall_thickness": WALL,
        "floor_thickness": FLOOR,
    }


def build_pockets(pockets, span_w: float, span_l: float, height: float,
                  out_stl: Path, corner_radius: float = 0.0, timeout: int = 180):
    """Generate a tray from an explicit pocket list. Returns (ok, error)."""
    out_stl = Path(out_stl)
    out_stl.parent.mkdir(parents=True, exist_ok=True)
    d = pocket_defines(pockets, span_w, span_l, height, corner_radius)
    return render.build_stl(SCAD, out_stl, d, timeout=timeout)


def fit_pockets(inner_w: float, inner_l: float, pockets, span_w: float,
                span_l: float, clearance: float) -> tuple:
    """Scale a pocket layout so the tray fills a box interior at an exact
    clearance per side (the pocket counterpart to fit_cells). Returns
    (scaled_pockets, new_span_w, new_span_l)."""
    target_w = inner_w - 2 * clearance - 2 * WALL
    target_l = inner_l - 2 * clearance - 2 * WALL
    fw = target_w / span_w if span_w else 1.0
    fl = target_l / span_l if span_l else 1.0
    scaled = [(round(x * fw, 3), round(y * fl, 3),
               round(w * fw, 3), round(l * fl, 3)) for x, y, w, l in pockets]
    return scaled, round(target_w, 3), round(target_l, 3)


def equal_cells(inner_w: float, inner_l: float, cols: int, rows: int,
                clearance: float) -> tuple:
    """Equal cell vectors that fit a box interior, minus the fit clearance.

    The equal-split counterpart to listing compartments: the tray fills the
    given interior (less `clearance` per side) with a uniform cols×rows grid.
    """
    span_w = inner_w - 2 * clearance - 2 * WALL
    span_l = inner_l - 2 * clearance - 2 * WALL
    cw = (span_w - (cols - 1) * DIVIDER) / cols
    cl = (span_l - (rows - 1) * DIVIDER) / rows
    return [round(cw, 3)] * cols, [round(cl, 3)] * rows


def build_equal(inner_w: float, inner_l: float, height: float, cols: int,
                rows: int, out_stl: Path, clearance: float = 0.15,
                corner_radius: float = 0.0, timeout: int = 180):
    """Build an equal cols×rows tray sized to a box interior. Returns (ok, err)."""
    col_widths, row_lengths = equal_cells(inner_w, inner_l, cols, rows, clearance)
    return build(col_widths, row_lengths, height, out_stl,
                 corner_radius=max(0.0, corner_radius - clearance), timeout=timeout)
