#!/usr/bin/env python3
"""Dimensioned engineering drawings for the framed-sandwich part family.

A windowed front plate + back plate joined by side dovetail rails, optionally
with a cantilever page-clip tongue and/or a through-cut logo. Edit PARAMS or
pass a JSON of the same keys:

    python3 make_drawings.py                  # built-in PARAMS
    python3 make_drawings.py mypart.json out_dir

Outputs <name>_drawings.pdf + one PNG per sheet. All dims in mm.

Sheets: front plan, back plan, side elevation, 3D iso views, detail+summary.

Optional keys (omit to skip): window_w/h, logo_d (+logo_x/y, logo_through),
clip (+clip_tw/clip_tl/clip_sw/clip_top_y/clip_corner/tip_t), pocket_depth.
3D iso sheet: set "scad" to a .scad path and "iso" to a list of [module,caption];
OpenSCAD renders each (skipped cleanly if OpenSCAD or the file is missing).
"""
import json, os, sys, shutil, subprocess, tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.image as mpimg
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.path import Path
from matplotlib.patches import PathPatch

PARAMS = {
    "name": "bookmark", "version": "v11b",
    "outer_w": 92, "outer_h": 112,
    "window_w": 68, "window_h": 88,
    "front_t": 1.4, "back_t": 0.8,
    "corner_r": 3, "win_r": 2, "edge_fillet": 0.3, "overlap": 6,
    "dt_narrow": 2.6, "dt_wide": 4.0, "dt_depth": 0.7, "dt_clear": 0.10, "rail_w": 6,
    "clip": True, "clip_tw": 68, "clip_tl": 30, "clip_sw": 2.2,
    "clip_top_y": 44, "clip_corner": 5, "tip_t": 0.30,
    "logo_through": True, "logo_d": 11, "logo_x": -35, "logo_y": -49.5,
    "scad": None, "iso": [],   # e.g. "scad":"bookmark_v11b.scad","iso":[["front_frame","Front"],["back_plate","Back"]]
}

PART, PARTE, FEAT = "#e8edf5", "#3a5d8f", "#cfdcec"
DIMC, HIDC = "#b23b3b", "#888888"


def rrect(w, h, r, cx=0, cy=0):
    x0, x1, y0, y1 = cx - w/2, cx + w/2, cy - h/2, cy + h/2
    k = r * 0.5523
    v = [(x0+r, y0), (x1-r, y0), (x1-r+k, y0), (x1, y0+r-k), (x1, y0+r),
         (x1, y1-r), (x1, y1-r+k), (x1-r+k, y1), (x1-r, y1),
         (x0+r, y1), (x0+r-k, y1), (x0, y1-r+k), (x0, y1-r),
         (x0, y0+r), (x0, y0+r-k), (x0+r-k, y0), (x0+r, y0)]
    c = [Path.MOVETO, Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
         Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
         Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
         Path.LINETO, Path.CURVE4, Path.CURVE4, Path.CURVE4]
    return Path(v, c)


def clip_outline(p, cx=0):
    """2D outline of the page-clip tongue: rounded bottom corners, square top."""
    hw = p["clip_tw"]/2; cc = p["clip_corner"]
    ytop = -(p["outer_h"]/2) + (p.get("clip_top_y_from_bottom") or
            (p["outer_h"]/2 + p["clip_top_y"]))  # clip_top_y is absolute in model coords
    # In the model, clip is at the TOP; for drawing we mirror to match plan (top=+y).
    return None  # plan sheets draw the clip explicitly below


def dimh(ax, x0, x1, y, t, ext=None, fs=7.5, c=DIMC):
    if ext is not None:
        for x in (x0, x1):
            ax.plot([x, x], [ext, y], lw=0.5, color=c, zorder=5)
    ax.annotate("", xy=(x1, y), xytext=(x0, y),
                arrowprops=dict(arrowstyle="<|-|>", lw=0.9, color=c, shrinkA=0, shrinkB=0,
                                mutation_scale=8), zorder=6)
    ax.text((x0+x1)/2, y, t, ha="center", va="center", fontsize=fs, color=c,
            bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none"), zorder=7)


def dimv(ax, y0, y1, x, t, ext=None, fs=7.5, c=DIMC):
    if ext is not None:
        for y in (y0, y1):
            ax.plot([ext, x], [y, y], lw=0.5, color=c, zorder=5)
    ax.annotate("", xy=(x, y1), xytext=(x, y0),
                arrowprops=dict(arrowstyle="<|-|>", lw=0.9, color=c, shrinkA=0, shrinkB=0,
                                mutation_scale=8), zorder=6)
    ax.text(x, (y0+y1)/2, t, ha="center", va="center", fontsize=fs, color=c, rotation=90,
            bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none"), zorder=7)


def leader(ax, x, y, tx, ty, t, fs=7.5, c=DIMC):
    ax.annotate("", xy=(x, y), xytext=(tx, ty),
                arrowprops=dict(arrowstyle="-|>", lw=0.8, color=c, mutation_scale=8), zorder=6)
    ax.text(tx, ty, t, ha="center", va="center", fontsize=fs, color=c,
            bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none"), zorder=7)


def titleblock(fig, p, title, sub, sheet, total):
    fig.text(0.5, 0.966, f"{p['name']} {p.get('version','')}  —  {title}",
             ha="center", fontsize=13, weight="bold")
    fig.text(0.5, 0.945, sub, ha="center", fontsize=8.5, color="#444")
    fig.text(0.01, 0.012, f"Sheet {sheet} of {total}  |  all dimensions mm  |  3rd-angle  |  "
             f"{p.get('version','')}", ha="left", fontsize=7, color="#777")


def _draw_clip_plan(ax, p):
    """Draw the page-clip tongue in plan (top view): U-slot + tongue at top edge."""
    oh = p["outer_h"]; hw = p["clip_tw"]/2; cc = p["clip_corner"]; sw = p["clip_sw"]
    ytop = p["clip_top_y"]; ybot = ytop - p["clip_tl"]
    # tongue rounded-bottom path
    import numpy as np
    bl = (-hw+cc, ybot+cc); br = (hw-cc, ybot+cc)
    pts = [(-hw, ytop)]
    pts += [(bl[0]+cc*np.cos(np.radians(a)), bl[1]+cc*np.sin(np.radians(a))) for a in range(180, 271, 10)]
    pts += [(br[0]+cc*np.cos(np.radians(a)), br[1]+cc*np.sin(np.radians(a))) for a in range(270, 361, 10)]
    pts += [(hw, ytop)]
    ax.add_patch(plt.Polygon(pts, closed=True, fc=FEAT, ec=PARTE, lw=1.1, zorder=4))
    # slot (outline offset) shown as a thin band
    op = [(-hw-sw, ytop)]
    op += [(bl[0]+(cc+sw)*np.cos(np.radians(a)), bl[1]+(cc+sw)*np.sin(np.radians(a))) for a in range(180, 271, 10)]
    op += [(br[0]+(cc+sw)*np.cos(np.radians(a)), br[1]+(cc+sw)*np.sin(np.radians(a))) for a in range(270, 361, 10)]
    op += [(hw+sw, ytop)]
    ax.add_patch(plt.Polygon(op, closed=True, fill=False, ec=HIDC, lw=0.7, ls=(0, (4, 2)), zorder=3))
    dimh(ax, -hw, hw, ytop + 6, f"{p['clip_tw']:g} clip", ext=ytop, fs=7)
    dimv(ax, ybot, ytop, hw + 8, f"{p['clip_tl']:g}", ext=hw, fs=7)
    leader(ax, 0, ybot, 12, ybot - 8, f"tongue: {p['tip_t']:g}→{p['back_t']:g} mm\nwedge taper", fs=6.5)


def sheet_front(p, n, total):
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ow, oh, cr, wr = p["outer_w"], p["outer_h"], p["corner_r"], p.get("win_r", 2)
    ax.add_patch(PathPatch(rrect(ow, oh, cr), fc=PART, ec=PARTE, lw=1.4))
    if p.get("window_w"):
        ww, wh = p["window_w"], p["window_h"]
        ax.add_patch(PathPatch(rrect(ww, wh, wr), fc="white", ec=PARTE, lw=1.4))
        dimh(ax, -ww/2, ww/2, wh/2 + 7, f"{ww:g}", ext=wh/2)
        dimv(ax, -wh/2, wh/2, -ww/2 - 7, f"{wh:g}", ext=-ww/2)
        ax.text(0, 14, f"WINDOW\n{ww:g} × {wh:g}", ha="center", va="center", fontsize=9, color="#999")
    xc = ow/2 - p["rail_w"]/2
    for s in (1, -1):
        ax.plot([s*xc, s*xc], [-oh/2, oh/2], lw=0.8, ls=(0, (8, 3, 2, 3)), color=HIDC, zorder=2)
    if p.get("logo_through"):
        lx, ly, ld = p["logo_x"], p["logo_y"], p["logo_d"]
        ax.add_patch(plt.Circle((lx, ly), ld/2, fc="#fde9c8", ec=PARTE, lw=1.0, zorder=3))
        ax.text(lx, ly, "T", ha="center", va="center", fontsize=11, weight="bold", color=PARTE, zorder=4)
        leader(ax, lx, ly - ld/2, lx - 6, ly - ld/2 - 9,
               f"logo ⌀{ld:g}\nCUT THROUGH\n(2nd colour shows)", fs=6.5)
    dimh(ax, -ow/2, ow/2, oh/2 + 14, f"{ow:g}", ext=oh/2)
    dimv(ax, -oh/2, oh/2, ow/2 + 14, f"{oh:g}", ext=ow/2)
    dimh(ax, 0, xc, oh/2 + 22, f"{xc:g} rail CL", ext=oh/2)
    leader(ax, ow/2 - cr*0.4, oh/2 - cr*0.4, ow/2 + 18, oh/2 + 2, f"R{cr:g}")
    leader(ax, ow/2, 6, ow/2 + 18, 12, f"R{p['edge_fillet']:g} fillet\n(exposed edges)", fs=7)
    ax.set_aspect("equal"); ax.axis("off")
    ax.set_xlim(-ow/2 - 36, ow/2 + 40); ax.set_ylim(-oh/2 - 30, oh/2 + 30)
    titleblock(fig, p, "Front Frame — plan (visible face)", f"Thickness {p['front_t']} mm", n, total)
    return fig


def sheet_back(p, n, total):
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ow, oh, cr = p["outer_w"], p["outer_h"], p["corner_r"]
    ax.add_patch(PathPatch(rrect(ow, oh, cr), fc=PART, ec=PARTE, lw=1.4))
    xc = ow/2 - p["rail_w"]/2
    tw = p["dt_wide"] - 2*p["dt_clear"]
    for s in (1, -1):
        ax.add_patch(plt.Rectangle((s*xc - tw/2, -oh/2), tw, oh, fc=FEAT, ec=PARTE, lw=1.0, zorder=3))
    if p.get("clip"):
        _draw_clip_plan(ax, p)
    vy = -((p.get("window_h", oh)/2) + 4)
    ax.text(0, vy, f'"{p.get("version","")}"', fontsize=7, ha="center", va="center", color=DIMC,
            bbox=dict(boxstyle="round", fc="#fff5f5", ec=DIMC, lw=0.6))
    leader(ax, 8, vy, 26, vy - 4, "version mark\n(mating face)", fs=7)
    dimh(ax, -ow/2, ow/2, oh/2 + 14, f"{ow:g}", ext=oh/2)
    dimv(ax, -oh/2, oh/2, ow/2 + 14, f"{oh:g}", ext=ow/2)
    dimh(ax, xc - tw/2, xc + tw/2, -oh/2 - 14, f"{tw:.1f} tongue", ext=-oh/2)
    leader(ax, ow/2 - cr*0.4, oh/2 - cr*0.4, ow/2 + 18, oh/2 + 2, f"R{cr:g}")
    ax.set_aspect("equal"); ax.axis("off")
    ax.set_xlim(-ow/2 - 36, ow/2 + 42); ax.set_ylim(-oh/2 - 30, oh/2 + 30)
    sub = "Vertical dovetail tongues both edges" + ("  |  cantilever page-clip tongue" if p.get("clip") else "")
    titleblock(fig, p, "Back Plate — plan (mating face)", f"Thickness {p['back_t']} mm  |  {sub}", n, total)
    return fig


def sheet_side(p, n, total):
    """Side elevation: assembled stack edge-on (looking along +x), thicknesses,
    dovetail engagement, clip taper. Heights exaggerated ×N for legibility."""
    fig, ax = plt.subplots(figsize=(11, 8.5))
    oh = p["outer_h"]; ft = p["front_t"]; bt = p["back_t"]
    SC = 14.0   # thickness exaggeration factor (annotated)
    fts, bts = ft*SC, bt*SC
    y0 = 0
    # back plate slab (z 0..bt) and front frame slab (z bt..bt+ft) drawn as side bars
    ax.add_patch(plt.Rectangle((-oh/2, y0), oh, bts, fc=PART, ec=PARTE, lw=1.3, zorder=2))
    # front frame: two border blocks (window gap in the middle)
    wh = p.get("window_h", 0)
    if wh:
        for (a, b) in [(-oh/2, -wh/2), (wh/2, oh/2)]:
            ax.add_patch(plt.Rectangle((a, y0+bts), b-a, fts, fc=PART, ec=PARTE, lw=1.3, zorder=2))
        ax.text(0, y0+bts+fts/2, "window gap", ha="center", va="center", fontsize=7, color="#999")
    else:
        ax.add_patch(plt.Rectangle((-oh/2, y0+bts), oh, fts, fc=PART, ec=PARTE, lw=1.3, zorder=2))
    # clip tongue taper (on the back plate, at the top edge region) — wedge profile
    if p.get("clip"):
        ytop = p["clip_top_y"]; ybot = ytop - p["clip_tl"]; tip = p["tip_t"]*SC
        ax.add_patch(plt.Polygon([(ybot, y0), (ytop, y0), (ytop, y0+bts), (ybot, y0+tip)],
                     closed=True, fc=FEAT, ec=PARTE, lw=1.0, zorder=3))
        leader(ax, (ybot+ytop)/2, y0+tip/2, (ybot+ytop)/2 - 6, y0 - 9*SC*0.0 - 7,
               f"clip wedge\n{p['tip_t']:g}→{p['back_t']:g} mm", fs=6.5)
    # dovetail rails at both ends (tongues poke up into front groove) — schematic
    dt = p["dt_depth"]*SC
    for s in (1, -1):
        xcap = s*oh/2
        ax.add_patch(plt.Rectangle((xcap - s*3 if s>0 else xcap, y0+bts - 0.1), 3, dt,
                     fc="#bcd0ea", ec=PARTE, lw=0.8, zorder=4))
    dimv(ax, y0, y0+bts, -oh/2 - 6, f"{bt:g} back", ext=-oh/2, fs=7)
    dimv(ax, y0+bts, y0+bts+fts, -oh/2 - 16, f"{ft:g} front", ext=-oh/2, fs=7)
    dimv(ax, y0, y0+bts+fts, oh/2 + 8, f"{round(ft+bt,2):g} stack", ext=oh/2, fs=7)
    dimh(ax, -oh/2, oh/2, -fts*0.5 - 6, f"{oh:g}", ext=y0)
    ax.text(0, y0+bts+fts+6, f"thickness shown ×{SC:g} for clarity", ha="center", fontsize=7.5,
            style="italic", color="#777")
    ax.set_aspect("equal"); ax.axis("off")
    ax.set_xlim(-oh/2 - 30, oh/2 + 26); ax.set_ylim(-fts - 16, y0+bts+fts + 16)
    titleblock(fig, p, "Side Elevation (assembled)",
               "Front frame seated on back plate; dovetail tongues engaged", n, total)
    return fig


def render_iso(scad, module, out_png, cam="0,0,0,55,0,25,0", size="900,760"):
    osc = shutil.which("openscad")
    if not osc or not scad or not os.path.exists(scad):
        return False
    wrap = tempfile.NamedTemporaryFile("w", suffix=".scad", delete=False, dir=os.path.dirname(out_png) or ".")
    wrap.write(f'use <{os.path.abspath(scad)}>\n{module}();\n'); wrap.close()
    runner = (["xvfb-run", "-a"] if not os.environ.get("DISPLAY") else []) + [osc]
    try:
        subprocess.run(runner + ["-o", out_png, f"--imgsize={size}", "--viewall", "--autocenter",
                                 "--projection=ortho", f"--camera={cam}", "--colorscheme=Tomorrow", wrap.name],
                       check=True, capture_output=True, timeout=240)
        return os.path.exists(out_png)
    except Exception:
        return False
    finally:
        try: os.unlink(wrap.name)
        except OSError: pass


def sheet_iso(p, out_dir, n, total):
    items = p.get("iso") or []
    imgs = []
    for mod, cap in items:
        png = os.path.join(out_dir, f"{p['name']}_iso_{mod}.png")
        if render_iso(p.get("scad"), mod, png):
            imgs.append((png, cap))
    if not imgs:
        return None
    fig = plt.figure(figsize=(11, 8.5))
    k = len(imgs)
    for i, (png, cap) in enumerate(imgs):
        ax = fig.add_axes([0.04 + i*(0.92/k), 0.10, 0.92/k - 0.02, 0.78])
        ax.imshow(mpimg.imread(png)); ax.axis("off")
        ax.set_title(cap, fontsize=10, weight="bold")
    titleblock(fig, p, "3D Views (isometric)", "Rendered from the OpenSCAD model", n, total)
    return fig


def sheet_detail(p, n, total):
    fig = plt.figure(figsize=(11, 8.5))
    gn, gw, gd, cl = p["dt_narrow"], p["dt_wide"], p["dt_depth"], p["dt_clear"]
    tn, tw, td = gn - 2*cl, gw - 2*cl, gd - 0.15
    ax = fig.add_axes([0.05, 0.55, 0.42, 0.34]); gx = 0.0
    ax.add_patch(plt.Rectangle((gx-5.5, 0), 11, gd+0.6, fc=PART, ec=PARTE, lw=1.2))
    ax.add_patch(plt.Polygon([(gx-gn/2, gd), (gx+gn/2, gd), (gx+gw/2, 0), (gx-gw/2, 0)],
                             closed=True, fc="white", ec=PARTE, lw=1.2))
    ax.add_patch(plt.Polygon([(gx-tn/2, gd-0.05), (gx+tn/2, gd-0.05), (gx+tw/2, 0.12), (gx-tw/2, 0.12)],
                             closed=True, fc=FEAT, ec=PARTE, lw=1.2))
    dimh(ax, gx-gn/2, gx+gn/2, gd+0.9, f"{gn:g} mouth", fs=7)
    dimh(ax, gx-gw/2, gx+gw/2, -0.65, f"{gw:g} foot", fs=7)
    dimv(ax, 0, gd, gx+gw/2+3, f"{gd:g} deep", fs=7)
    leader(ax, gx+tw/2+0.05, gd/2, gx+8.5, gd/2+0.6, f"{cl:.2f} mm\nclearance\nper side", fs=7)
    ax.text(gx, gd+1.6, "DETAIL  dovetail engagement", fontsize=8.5, weight="bold", ha="center")
    ax.set_aspect("equal"); ax.axis("off"); ax.set_xlim(gx-10, gx+15); ax.set_ylim(-1.5, gd+2.4)

    ax = fig.add_axes([0.52, 0.08, 0.44, 0.80]); ax.axis("off")
    rows = [
        ("Overall footprint", f"{p['outer_w']:g} × {p['outer_h']:g} mm"),
        ("Window", f"{p['window_w']:g} × {p['window_h']:g} mm" if p.get("window_w") else "—"),
        ("Front thickness", f"{p['front_t']} mm"),
        ("Back thickness", f"{p['back_t']} mm"),
        ("Assembled stack", f"{round(p['front_t']+p['back_t'],2)} mm"),
        ("Dovetail groove", f"{gn:g}/{gw:g} × {gd:g} deep"),
        ("Dovetail tongue", f"{tn:.1f}/{tw:.1f} × {td:.2f} deep"),
        ("Clearance / side", f"{cl:.2f} mm  (frozen)"),
        ("Rail centerline", f"±{p['outer_w']/2 - p['rail_w']/2:g} mm"),
        ("Edge fillet", f"R {p['edge_fillet']} mm  (exposed only)"),
    ]
    if p.get("clip"):
        rows += [("Page clip", f"{p['clip_tw']:g} × {p['clip_tl']:g} mm tongue, {p['clip_sw']:g} slot"),
                 ("Clip taper", f"{p['tip_t']:g} → {p['back_t']:g} mm (hinge full-thickness)")]
    if p.get("logo_through"):
        rows += [("Logo", f"⌀{p['logo_d']:g} CUT THROUGH (front, 2-colour)")]
    rows += [("Version mark", f"\"{p.get('version','')}\" engraved, mating face")]
    sy, rh = 0.94, 0.058
    ax.text(0.03, sy+0.03, "PARAMETER", weight="bold", fontsize=8, transform=ax.transAxes)
    ax.text(0.52, sy+0.03, "VALUE", weight="bold", fontsize=8, transform=ax.transAxes)
    ax.plot([0.02, 0.98], [sy+0.008, sy+0.008], color="#aaa", lw=0.7, transform=ax.transAxes)
    for i, (kk, vv) in enumerate(rows):
        y = sy - (i+1)*rh
        if i % 2 == 0:
            ax.add_patch(mpatches.FancyBboxPatch((0.02, y+0.006), 0.96, rh-0.006, boxstyle="square,pad=0",
                         fc="#f5f7fa", ec="none", transform=ax.transAxes, zorder=0))
        ax.text(0.03, y+rh*0.4, kk, fontsize=7.5, transform=ax.transAxes)
        ax.text(0.52, y+rh*0.4, vv, fontsize=7.5, transform=ax.transAxes, color="#333")
    titleblock(fig, p, "Dovetail Detail & Summary",
               "Mating/joint surfaces kept crisp — fillet applies to exposed edges only", n, total)
    return fig


def main():
    p = dict(PARAMS); out_dir = "."
    args = sys.argv[1:]
    if args and args[0].endswith(".json"):
        with open(args[0]) as f: p.update(json.load(f))
        args = args[1:]
    if args: out_dir = args[0]
    os.makedirs(out_dir, exist_ok=True)

    figs = []
    # build iso first (may be skipped); decide total sheets accordingly
    plan = [("sheet1_front", sheet_front), ("sheet2_back", sheet_back), ("sheet3_side", sheet_side)]
    iso_fig = None
    # tentative total: 3 plan + detail (+iso if it renders)
    iso_items = p.get("iso") or []
    will_iso = bool(iso_items and shutil.which("openscad") and p.get("scad") and os.path.exists(p["scad"]))
    total = len(plan) + 1 + (1 if will_iso else 0)

    n = 1
    for tag, fn in plan:
        fig = fn(p, n, total); figs.append((tag, fig)); n += 1
    if will_iso:
        f = sheet_iso(p, out_dir, n, total)
        if f: figs.append(("sheet4_iso", f)); n += 1
    figs.append((f"sheet{n}_detail", sheet_detail(p, n, total)))

    for tag, fig in figs:
        fig.savefig(os.path.join(out_dir, f"{p['name']}_{tag}.png"), dpi=140, bbox_inches="tight")
    pdf_path = os.path.join(out_dir, f"{p['name']}_drawings.pdf")
    with PdfPages(pdf_path) as pdf:
        for _, fig in figs:
            pdf.savefig(fig, bbox_inches="tight")
    print(f"wrote {pdf_path} (+ {len(figs)} PNG sheets)")


if __name__ == "__main__":
    main()
