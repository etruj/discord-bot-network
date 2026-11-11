"""Ballooned top-view feature map — the drawing the user edits from.

Generated automatically for any traced part:

* Letter-landscape sheet: title, notes line, part filled + heavy outline,
  origin marker, ℄ mirror line(s), overall dimensions.
* Balloons with leaders: E# edges (blue box), C# corners (red circle),
  H# holes (green circle). For a symmetric part only the kept half
  (right / top) is ballooned; mirror partners are E#′ and change together.
* Two-column table: label, what it is, exact geometry in the .scad frame
  (origin = part's bottom-left, mm) — e.g. "Y = 26.0;  X 10.7 → 59.2",
  "R10.0, center (…)", "Ø6.0 thru;  center (…)".

Writes drawing.png + drawing.pdf.
"""
from __future__ import annotations

import math
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
import numpy as np                                    # noqa: E402
from matplotlib.patches import PathPatch              # noqa: E402
from matplotlib.path import Path                      # noqa: E402
from shapely.geometry import Point, Polygon           # noqa: E402

from .refine import Joint, Ring, Shape                # noqa: E402

C_EDGE, C_CORN, C_HOLE = "#1f5fbf", "#c0392b", "#1e8449"
FILL = "#f4f1ea"


def _poly_path(outer, holes):
    verts, codes = [], []
    for ring in [outer, *holes]:
        ring = np.asarray(ring)
        verts += list(ring) + [ring[0]]
        codes += [Path.MOVETO] + [Path.LINETO] * (len(ring) - 1) + [Path.CLOSEPOLY]
    return Path(verts, codes)


def _anchor_dir(o, part: Polygon, L, W):
    """(anchor point on the feature, outward unit direction for the balloon)."""
    c = np.array(part.centroid.coords[0])
    if isinstance(o, Ring):
        gm = o.geom
        ctr = np.asarray(gm.get("c", o.centre))
        r = gm.get("r") or (gm.get("a", 0) / 2) or max(gm.get("w", 4), gm.get("h", 4)) / 2
        d = np.array([math.cos(math.radians(60)), math.sin(math.radians(60))])
        return ctr + d * r, d
    if isinstance(o, Joint):
        fallback = o.out[-1] if o.out is not None and len(o.out) else o.at
        p = np.asarray(o.geom.get("at", fallback), float)
        if o.geom.get("type") == "radius":
            pts = np.asarray(o.out)
            p = pts[len(pts) // 2]
    else:
        pts = np.asarray(o.out if o.out is not None else o.pts)
        p = pts[len(pts) // 2]
        if len(pts) == 2:
            p = pts.mean(axis=0)
    # outward = away from the solid: probe both normals / the centroid ray
    cands = []
    if not isinstance(o, Joint) and o.out is not None and len(o.out) >= 2:
        pts = np.asarray(o.out)
        i = len(pts) // 2
        t = pts[min(i + 1, len(pts) - 1)] - pts[max(i - 1, 0)]
        t = t / max(np.hypot(*t), 1e-9)
        cands += [np.array([t[1], -t[0]]), np.array([-t[1], t[0]])]
    ray = p - c
    cands.append(ray / max(np.hypot(*ray), 1e-9))
    eps = 0.02 * max(L, W)
    for d in cands:
        if not part.buffer(-1e-6).contains(Point(*(p + d * eps))):
            return p, d
    return p, cands[-1]


def _place(items, part, sep, dist, bounds):
    """Balloon positions: start along each feature's outward direction, then
    push apart until none overlap (edge/corner balloons stay off the part)."""
    pos = np.array([a + d * (dist if kind != "H" else dist * 0.7)
                    for kind, a, d in items], float)
    grown = part.buffer(sep * 0.45)
    x0, y0, x1, y1 = bounds
    for _ in range(200):
        moved = False
        for i in range(len(pos)):
            for j in range(i + 1, len(pos)):
                v = pos[i] - pos[j]
                dd = np.hypot(*v)
                if dd < sep:
                    v = v / dd if dd > 1e-9 else np.array([1.0, 0.0])
                    push = (sep - dd) / 2 + 1e-3
                    pos[i] += v * push
                    pos[j] -= v * push
                    moved = True
        for i, (kind, a, d) in enumerate(items):
            if kind != "H" and grown.contains(Point(*pos[i])):
                pos[i] += d * sep * 0.25
                moved = True
            pos[i] = np.clip(pos[i], [x0, y0], [x1, y1])
        if not moved:
            break
    return pos


def _compact(v: str) -> str:
    """Short form of a table value for the changes list."""
    v = v.replace(" thru", "").replace("center ", "@ ")
    parts = [x.strip() for x in v.split(";")]
    head = parts[0]
    if len(parts) > 1 and parts[1].startswith("@"):          # hole: size + position
        head += " " + parts[1]
    m = re.search(r"\((\d+(\.\d+)?) long\)", v)
    return head + (f" ({m.group(1)} long)" if m and "long" not in head else "")


def _changes(rows, prev_rows):
    out = []
    now = {r["label"]: r["value"] for r in rows}
    for lab, v in now.items():
        old = prev_rows.get(lab)
        if old is not None and old != v:
            out.append(f"{lab}:  {_compact(old)}  →  {_compact(v)}")
    for lab in prev_rows:
        if lab not in now:
            out.append(f"{lab}:  removed")
    for r in rows:
        if r["kind"] == "hole" and r["desc"].endswith("(removed)") and \
                prev_rows.get(r["label"]) not in (None, "—"):
            out.append(f"{r['label']}:  removed")
    return list(dict.fromkeys(out))


def render(shape: Shape, outer, holes, png: str, pdf: str | None, *, name: str,
           version: int, thickness: float, notes: str = "", requirements=(),
           locked=frozenset(), prev=None, finish=None) -> None:
    L, W = shape.length, shape.width
    part = Polygon(outer, holes)
    M = 0.30 * max(L, W)
    fig = plt.figure(figsize=(17, 11))
    AXW = 0.66                                    # right side: requirements / changes
    ax = fig.add_axes([0.03, 0.31, AXW, 0.60])
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_xlim(-M, L + M)
    ax.set_ylim(-M, W + M)
    # inches per mm with equal aspect -> balloon size in mm
    scale = min(AXW * 17 / (L + 2 * M), 0.60 * 11 / (W + 2 * M))
    sep = 0.42 / scale

    if prev is not None:                          # ghost of the previous version
        ax.add_patch(PathPatch(_poly_path(prev["outer"], prev["holes"]), fc="none",
                               ec="#e67e22", lw=1.4, ls=(0, (6, 3)), zorder=4))
    ax.add_patch(PathPatch(_poly_path(outer, holes), fc=FILL, ec="black", lw=2.2, zorder=2))
    for h in shape.holes:                              # centre marks on round holes
        gm = h.geom
        if gm.get("type") in ("circle", "oval"):
            c, r = gm["c"], gm.get("r", gm.get("a", 0) / 2)
            kw = dict(color="0.5", lw=0.6, ls=(0, (8, 2, 2, 2)), zorder=5)
            ax.plot([c[0] - 1.35 * r, c[0] + 1.35 * r], [c[1]] * 2, **kw)
            ax.plot([c[0]] * 2, [c[1] - 1.35 * r, c[1] + 1.35 * r], **kw)
    for f in shape.fastener_cuts():                    # screw-head recess / nut pocket
        for kind, size, _, face in f["cuts"]:
            # dashed = top face, dotted = bottom face (hidden from this view)
            ls = (0, (5, 2)) if face == "top" else (0, (1.5, 1.5))
            if kind == "hex":
                rr = size / 2 / np.cos(np.radians(30))
                a = np.radians(np.arange(0, 361, 60))
                ax.plot(f["x"] + rr * np.cos(a), f["y"] + rr * np.sin(a),
                        color="#1e8449", lw=1.1, ls=ls, zorder=5)
            else:
                ax.add_patch(plt.Circle((f["x"], f["y"]), size / 2, fc="none",
                                        ec="#1e8449", lw=1.1, ls=ls, zorder=5))

    sym_txt = []
    for a in shape.mirrors:
        kw = dict(color="0.35", lw=1, ls=(0, (12, 3, 2, 3)), zorder=3)
        if a == "x":
            ax.plot([L / 2] * 2, [-0.6 * M, W + 0.6 * M], **kw)
            ax.text(L / 2, W + 0.65 * M, f"℄  X = {L / 2:.1f} (mirror line)",
                    ha="center", fontsize=10, color="0.3")
            sym_txt.append("left half")
        else:
            ax.plot([-0.6 * M, L + 0.6 * M], [W / 2] * 2, **kw)
            ax.text(-0.62 * M, W / 2, f"℄ Y = {W / 2:.1f}", ha="right", va="center",
                    fontsize=10, color="0.3")
            sym_txt.append("bottom half")

    # balloons for primaries only
    items, objs = [], []
    for o in shape._objects():
        if not o.primary or o.label in [x.label for x in objs]:
            continue
        if isinstance(o, Ring) and o.geom.get("type") == "removed":
            continue
        if not isinstance(o, (Ring, Joint)) and o.out is None:
            continue                                   # dropped corner seg
        a, d = _anchor_dir(o, part, L, W)
        items.append((o.label[0], a, d))
        objs.append(o)
    pos = _place(items, part, sep, max(0.1 * max(L, W), 1.4 * sep),
                 (-M * 0.95, -M * 0.75, L + M * 0.95, W + M * 0.85))
    rows = shape.legend()
    changes = _changes(rows, prev["rows"]) if prev else []
    changed = {c.split(":")[0] for c in changes}
    for (kind, a, _), p, o in zip(items, pos, objs):
        col = {"E": C_EDGE, "C": C_CORN, "H": C_HOLE}[kind]
        # filled = locked by a requirement; yellow = changed since last version
        is_lock, is_new = o.label in locked, o.label in changed
        box = dict(boxstyle="round,pad=0.3" if kind == "E" else "circle,pad=0.25",
                   fc=col if is_lock else ("#fff1b8" if is_new else "white"),
                   ec="#e67e22" if is_new else col, lw=2.6 if is_new else 1.8)
        ax.annotate(o.label, xy=a, xytext=p, ha="center", va="center", fontsize=11,
                    weight="bold", color="white" if is_lock else col, bbox=box, zorder=10,
                    arrowprops=dict(arrowstyle="-|>", color=col, lw=1.1, shrinkA=0,
                                    shrinkB=1, mutation_scale=10))

    # overall dimensions + origin
    dim = dict(arrowstyle="<->", color="0.25", lw=0.9)
    yb = -0.8 * M
    ax.annotate("", (0, yb), (L, yb), arrowprops=dim)
    ax.text(L / 2, yb - 0.03 * M, f"{L:.1f}", ha="center", va="top", fontsize=10)
    xr = L + 0.85 * M
    ax.annotate("", (xr, 0), (xr, W), arrowprops=dim)
    ax.text(xr + 0.03 * M, W / 2, f"{W:.1f}", rotation=90, va="center", fontsize=10)
    ext = dict(color="0.6", lw=0.5)
    for x in (0, L):
        ax.plot([x, x], [yb - 0.02 * M, -0.02 * M], **ext)
    for y in (0, W):
        ax.plot([L + 0.02 * M, xr + 0.02 * M], [y, y], **ext)
    ax.plot(0, 0, "k+", ms=12, mew=1.2, zorder=6)
    ax.text(0.01 * M, -0.08 * M, "origin (0,0)", fontsize=8, color="0.3")

    sym = shape.mirrors
    title = f"{name.upper()} v{version} — TOP VIEW — FEATURE MAP"
    if sym:
        title += "  (symmetric: " + " + ".join(
            {"x": "about ℄ X", "y": "about ℄ Y"}[a] for a in sym) + ")"
    fig.text(0.03, 0.965, title, fontsize=15, weight="bold")
    note = (f"All dims mm. Origin = bottom-left of the part (same frame as {name}.scad). "
            f"+X right, +Y up. Extruded {thickness:g} mm, printed flat. ")
    if sym:
        note += (f"Only one side is labelled; the {' and '.join(sym_txt)} "
                 f"{'is' if len(sym_txt) == 1 else 'are'} the mirror image (E#′ / C#′ / H#′) "
                 f"and change together. ")
    note += ("Reply with label + change, e.g. 'E3 up 2', 'E1 straight', 'E4 out 1', "
             "'C2 R5', 'C4 sharp', 'H1 circle 6', 'H1 left 3', 'H2 remove'. " + notes)
    fig.text(0.03, 0.935, note, fontsize=9, wrap=True, va="top")

    # right panel
    px, py = 0.715, 0.885
    def line(txt, **kw):
        nonlocal py
        fig.text(px, py, txt, va="top", **kw)
        py -= 0.022 if kw.get("fontsize", 9) <= 9 else 0.027
    line("REQUIREMENTS  (kept in every version)", fontsize=10, weight="bold")
    for r in requirements or ["none yet"]:
        line(f"■  {r}", fontsize=9)
    line("Filled balloons = locked by a requirement.", fontsize=8, color="0.4")
    py -= 0.01
    if finish:
        line("EDGE FINISH", fontsize=10, weight="bold")
        for side, (k, r) in finish.items():
            line(f"■  {side.lower()} edges {'R' if k == 'fillet' else 'chamfer '}{r:g}",
                 fontsize=9)
        py -= 0.01
    if prev is not None:
        line(f"CHANGES FROM v{prev['version']}", fontsize=10, weight="bold", color="#b9600f")
        for c in changes[:12] or ["no geometry changes"]:
            line(c, fontsize=8.5, family="monospace")
        if len(changes) > 12:
            line(f"… and {len(changes) - 12} more", fontsize=8.5)
        line(f"Dashed orange = v{prev['version']} outline; yellow balloons changed.",
             fontsize=8, color="0.4")

    tab = fig.add_axes([0.03, 0.01, 0.94, 0.27])
    tab.axis("off")
    cols = 2 if len(rows) <= 28 else 3
    per = max(1, math.ceil(len(rows) / cols))
    fs = min(9.5, 9.5 * 13 / max(per, 13))
    w = 1 / cols
    for i, r in enumerate(rows):
        col, row = divmod(i, per)
        x0, y = col * w, 1 - (row + 0.6) / max(per, 13)
        c = {"E": C_EDGE, "C": C_CORN, "H": C_HOLE}[r["label"][0]]
        tab.text(x0, y, r["label"] + ("■" if r["label"] in locked else ""), color=c,
                 weight="bold", fontsize=fs + 0.5, va="center", transform=tab.transAxes)
        tab.text(x0 + 0.035 * 2 / cols, y, r["desc"], fontsize=fs, va="center",
                 transform=tab.transAxes)
        tab.text(x0 + 0.17 * 2 / cols, y, r["value"], fontsize=fs, va="center",
                 family="monospace", transform=tab.transAxes)

    fig.savefig(png, dpi=120)
    if pdf:
        fig.savefig(pdf)
    plt.close(fig)
