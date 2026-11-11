#!/usr/bin/env python3
"""render.py — turning generated STLs into pictures.

Everything here is box-type agnostic: run OpenSCAD headless, lay parts out on a
grid, frame the shot, crop it. Anything that needs to know how a particular box
goes together (which way the lid flips, where the hinge axis sits) belongs in
that box type's module, which passes the assembled scene in as SCAD source.
"""
import math
import os
import shutil
import subprocess
from pathlib import Path

# Part colours. Unknown parts fall back to grey rather than failing.
COLORS = {"bottom": "#e07a2f", "top": "#7ab8f0", "latch": "#546074",
          "handle": "#546074", "label": "#c8d0dc",
          "insert": "#6fbf73", "body": "#e07a2f", "lid": "#7ab8f0"}
FALLBACK_COLOR = "#8a8a8a"


def have_openscad() -> bool:
    return bool(shutil.which("openscad"))


def _fmt_define(v) -> str:
    """Render one value for OpenSCAD's `-D key=value`.

    Strings must arrive quoted (so `render_part=box` reaches SCAD as the string
    "box", not an undefined variable); bools use SCAD's lowercase spelling; and
    lists become OpenSCAD vectors (`[40,20]`), which is how cell sizes reach the
    cubby generator.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_fmt_define(x) for x in v) + "]"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _define_args(defines: dict) -> list:
    args = []
    for k, v in (defines or {}).items():
        args += ["-D", f"{k}={_fmt_define(v)}"]
    return args


def _openscad_env():
    """The headless env `shoot()` established, factored out for reuse."""
    env = {**os.environ}
    if not shutil.which("xvfb-run") and not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"
    return env


def build_stl(scad_path, out_stl, defines: dict = None, timeout: int = 300):
    """Render a .scad file to an STL with -D overrides. Returns (ok, error).

    This is the STL counterpart to `shoot()`: box types that ship their own
    parametric SCAD (sliding, cubby) drive it through here instead of the
    vendored box-builder CLI. Geometry, not a picture, so no camera flags.
    """
    out_stl = Path(out_stl)
    cmd = ["openscad", "-o", str(out_stl), *_define_args(defines), str(scad_path)]
    if shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=_openscad_env())
    except subprocess.TimeoutExpired:
        return False, f"openscad timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001 — report, don't crash the build
        return False, str(e)
    if out_stl.exists() and out_stl.stat().st_size > 0:
        return True, ""
    tail = ((r.stderr or r.stdout or "").strip().splitlines()
            or ["(no output)"])[-4:]
    return False, " ".join(tail)


def shoot_scad_file(scad_path, png: Path, defines: dict, cam: str,
                    imgsize: str, timeout: int = 300):
    """Render an existing .scad *file* to a PNG with -D overrides.

    `shoot()` renders SCAD source we generate in memory (assembled STL scenes);
    this renders a SCAD file directly, letting a box type reuse its own SCAD's
    layout (e.g. sliding's `render_part=assembled/both`) instead of
    re-composing parts from STLs. Returns (ok, error_message).
    """
    png = Path(png)
    try:
        cmd = ["openscad", "-o", str(png), f"--imgsize={imgsize}",
               "--viewall", "--autocenter", "--projection=ortho",
               f"--camera={cam}", "--colorscheme=Tomorrow",
               *_define_args(defines), str(scad_path)]
        if shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=_openscad_env())
        if png.exists():
            autocrop(png)
            return True, ""
        tail = ((r.stderr or "").strip().splitlines() or ["(no output)"])[-3:]
        return False, " ".join(tail)
    except Exception as e:  # noqa: BLE001 — renders are a bonus, not the build
        return False, str(e)


def stl_import(path, part: str = "", color: str = "") -> str:
    """One coloured import() statement."""
    c = color or COLORS.get(part, FALLBACK_COLOR)
    return f'color("{c}") import("{path}", convexity=10);'


def canvas(w_mm: float, d_mm: float, h_mm: float, rx: float,
           base_w: int = 3200) -> str:
    """Pick an --imgsize matching the scene's on-screen proportions.

    OpenSCAD's --viewall fits the bounding sphere with a wide margin, so the
    model covers roughly 40% of the frame. We render ~3x the target size and
    let autocrop() bring it back, which yields a crisp, tightly framed image
    without having to calibrate a camera distance per box size.
    """
    proj_h = d_mm * math.cos(math.radians(rx)) + h_mm * math.sin(math.radians(rx))
    aspect = max(0.9, min(3.2, w_mm / max(proj_h, 1e-6)))
    height = int(max(1100, min(2000, base_w / aspect)))
    return f"{base_w},{height}"


def autocrop(path: Path, margin: int = 28):
    """Trim the flat background OpenSCAD leaves around the model."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return
    try:
        with Image.open(path) as src:
            im = src.convert("RGB")
        bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
        bbox = ImageChops.difference(im, bg).getbbox()
        if not bbox:
            return
        left, top, right, bottom = bbox
        im.crop((max(0, left - margin), max(0, top - margin),
                 min(im.width, right + margin),
                 min(im.height, bottom + margin))).save(path)
    except Exception:  # noqa: BLE001 — cropping is cosmetic, keep the render
        pass


def shoot(src: str, png: Path, cam: str, imgsize: str, timeout: int = 300):
    """Render SCAD source to a PNG. Returns (ok, error_message)."""
    scad = png.with_suffix(".scad.tmp")
    try:
        scad.write_text(src)
        cmd = ["openscad", "-o", str(png), f"--imgsize={imgsize}",
               "--viewall", "--autocenter", "--projection=ortho",
               f"--camera={cam}", "--colorscheme=Tomorrow", str(scad)]
        if shutil.which("xvfb-run"):        # works headless / logged out
            cmd = ["xvfb-run", "-a"] + cmd
        env = {**os.environ}
        if not shutil.which("xvfb-run") and not env.get("DISPLAY"):
            env["DISPLAY"] = ":0"
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        if png.exists():
            autocrop(png)
            return True, ""
        tail = ((r.stderr or "").strip().splitlines() or ["(no output)"])[-3:]
        return False, " ".join(tail)
    except Exception as e:  # noqa: BLE001 — renders are a bonus, not the build
        return False, str(e)
    finally:
        scad.unlink(missing_ok=True)


def parts_grid(files: dict, bounds: dict, order: list, pad: float = 14.0):
    """Lay every part flat on z=0, packed into rows rather than one long line
    (a single row renders as a thin strip with the parts too small to read).

    Returns (scad_source, total_width, total_depth, tallest).
    """
    order = [p for p in order if p in bounds]
    size = {p: (bounds[p][1][0] - bounds[p][0][0],
                bounds[p][1][1] - bounds[p][0][1]) for p in order}
    row_limit = sum(size[p][0] for p in order[:2]) + pad   # two biggest per row
    rows, cur, cur_w = [], [], 0.0
    for p in order:
        w = size[p][0]
        if cur and cur_w + pad + w > row_limit * 1.05:
            rows.append(cur); cur, cur_w = [], 0.0
        cur_w += (pad if cur else 0) + w
        cur.append(p)
    if cur:
        rows.append(cur)

    laid, y, total_w = [], 0.0, 0.0
    for row in rows:
        row_w = sum(size[p][0] for p in row) + pad * (len(row) - 1)
        row_d = max(size[p][1] for p in row)
        total_w = max(total_w, row_w)
        x = -row_w / 2
        for p in row:
            b = bounds[p]
            laid.append(f'translate([{x - b[0][0]:.2f},'
                        f'{y - b[0][1] - row_d / 2:.2f},{-b[0][2]:.2f}]) '
                        + stl_import(files[p], p))
            x += size[p][0] + pad
        y -= row_d + pad

    total_d = -y
    src = (f"translate([0,{total_d / 2 - pad / 2:.2f},0]) {{\n"
           + "\n".join(laid) + "\n}")
    tallest = max(bounds[p][1][2] - bounds[p][0][2] for p in order)
    return src, total_w, total_d, tallest


def layout_png(pockets, span_w: float, span_l: float, out_path,
               title: str = "") -> Path:
    """Draw a fast top-down schematic of a compartment layout, cells labelled.

    Not an OpenSCAD render — pure PIL, ~instant — so the layout step can show a
    picture on every edit. Takes the canonical pocket list ([x, y, w, l], mm,
    interior-local) so it draws grids and ragged layouts alike. Returns the path
    (or None if PIL is unavailable).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    out_path = Path(out_path)
    scale = 900.0 / max(span_w, span_l, 1e-6)        # px per mm, ~900 px long side
    margin, top = 46, (68 if title else 34)          # room for title + axis legend
    W = int(span_w * scale) + 2 * margin
    H = int(span_l * scale) + margin + top

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 20)
        tfont = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
    except OSError:
        font = tfont = ImageFont.load_default()

    img = Image.new("RGB", (W, H), "#f4f4f4")
    d = ImageDraw.Draw(img)
    if title:
        d.text((margin, 12), title, fill="#222222", font=tfont)

    # Solid tray body first, so any space between/around cells reads as wall
    # (thicker material), not an empty void. Pockets are the open compartments.
    d.rectangle([margin, top, margin + span_w * scale, top + span_l * scale],
                fill="#7d9b80", outline="#2f6f3f", width=3)
    for x, y, w, l in pockets:
        x1, y1 = margin + x * scale, top + y * scale
        x2, y2 = x1 + w * scale, y1 + l * scale
        d.rectangle([x1, y1, x2, y2], fill="#eef6ee", outline="#2f6f3f", width=3)
        # Each cell reads "width x length" (side-to-side x front-to-back).
        d.text(((x1 + x2) / 2, (y1 + y2) / 2), f"{w:g} x {l:g}",
               fill="#204020", font=font, anchor="mm")

    # Axis legend so the picture teaches which way is width vs length.
    d.text((margin, top - 26), "<-- width (side-to-side) -->",
           fill="#666666", font=font)
    d.text((margin, H - 24),
           "length = front-to-back (top = back) - each cell: width x length",
           fill="#888888", font=font)
    img.save(out_path)
    return out_path


def load_bounds(files: dict):
    """Bounding box per part via trimesh. Returns (meshes, bounds, warnings)."""
    try:
        import trimesh
    except ImportError:
        return {}, {}, ["`trimesh` not installed — skipped the renders."]
    meshes, bounds, warns = {}, {}, []
    for part, path in files.items():
        try:
            m = trimesh.load(path)
            meshes[part], bounds[part] = m, m.bounds
        except Exception as e:  # noqa: BLE001 — a bad STL shouldn't kill the build
            warns.append(f"could not read `{Path(path).name}`: {e}")
    return meshes, bounds, warns
