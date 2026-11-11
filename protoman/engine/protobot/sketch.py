"""Bot option 1 — TRACINGS: a drawn silhouette (pen/marker on paper, or a
digital drawing) -> clean silhouette mask -> outline polygons.

For photos of the real object itself use photo.py instead; the two are kept
separate on purpose so tuning one can't break the other.

Handles the two ways people draw a silhouette:

* **filled** — the shape is coloured in (marker, digital fill, a cut-out
  photographed on paper). Ink = solid, enclosed paper = holes.
* **outline** — only the edge is drawn. Regions are filled by the even-odd
  rule: inside one line = solid, inside a line drawn inside that = a hole, and
  so on. The result is then eroded by half the stroke width so the edge lands
  on the pen-line's centre, not its outer side.

`auto` (default) picks between them by how much of the outer shape is ink.

Photos of drawings are flattened first (divide by a fitted paper-brightness
surface) so shadows and uneven lighting don't become part of the shape.
"""
from __future__ import annotations

import cv2
import numpy as np

from .common import (NOISE_FRAC, Trace, fit_surface, load_gray, mask_to_polys,
                     smooth_mask)

# Below this ink/filled-area ratio, an `auto` sketch is treated as an outline.
OUTLINE_INK_RATIO = 0.45


def _local_kernel(shape) -> int:
    """Background window for local flattening: wider than any pen line,
    narrow enough to follow a hard-edged shadow (~4% of the image)."""
    return int(max(31, 0.04 * max(shape))) | 1


def ink_mask(gray: np.ndarray, invert: bool = False,
             local: bool = False) -> np.ndarray:
    """Threshold to ink (255) vs paper (0), robust to shadows / lighting.

    local=False fits one smooth (quadratic) paper surface: handles coloured-in
    shapes of any size, but a hard-edged cast shadow (phone, hand) defeats it
    — shaded paper can be darker than lit ink.
    local=True takes the paper brightness from a grey closing (max then min
    filter) a few pen-widths wide: thin lines vanish from the background, a
    shadow edge doesn't, so lines stand out even across the shadow. Big
    filled areas only keep their edges, so it suits outline drawings."""
    g = gray.astype(np.float32)
    if invert:                       # light drawing on a dark background
        g = 255 - g
    g = cv2.GaussianBlur(g, (5, 5), 0)
    if local:
        k = _local_kernel(g.shape)
        bg = cv2.morphologyEx(g, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        flat = np.clip(g / np.maximum(bg, 1) * 235, 0, 255).astype(np.uint8)
    else:
        # Flatten lighting: fit a smooth (quadratic) surface to the paper
        # pixels and divide it out. Unlike a morphological background
        # estimate this works no matter how large the coloured-in shape is.
        flat = g.astype(np.uint8)
        for _ in range(2):
            _, m = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            bg = fit_surface(g, m == 0)
            flat = np.clip(g / np.maximum(bg, 1) * 235, 0, 255).astype(np.uint8)
    _, m = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Otsu on a blank-ish page can split paper grain; demand real contrast.
    if np.count_nonzero(m) and (flat[m == 0].mean() - flat[m > 0].mean()) < 25:
        return np.zeros_like(m)
    # Remove speckle, bridge tiny gaps in pen lines.
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return m


def _drop_border_junk(ink: np.ndarray, warnings: list[str]) -> np.ndarray:
    """Remove blobs touching the image edge (table, shadows, page edge) —
    but only when a sizeable blob that doesn't touch the edge exists, so a
    tightly cropped drawing isn't thrown away."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if n <= 2:
        return ink
    h, w = ink.shape
    x, y, bw, bh, area = (stats[:, i] for i in range(5))
    touches = (x == 0) | (y == 0) | (x + bw >= w) | (y + bh >= h)
    touches[0] = False
    inner = [i for i in range(1, n) if not touches[i]]
    if not inner or not touches[1:].any():
        return ink
    biggest_inner = max(area[i] for i in inner)
    if biggest_inner < 0.01 * h * w:
        return ink
    out = ink.copy()
    for i in range(1, n):
        if touches[i]:
            out[lab == i] = 0
    warnings.append("ignored dark regions touching the photo edge "
                    "(table/shadow) — crop tighter if that was part of the shape")
    return out


def _outer_region(ink: np.ndarray) -> np.ndarray | None:
    """Filled outer contour of the widest-spread ink blob (largest convex
    hull, so an outline with a gap still beats a closed hole drawn inside
    it). None if there's no ink."""
    cnts, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    out = np.zeros_like(ink)
    big = max(cnts, key=lambda c: cv2.contourArea(cv2.convexHull(c)))
    cv2.drawContours(out, [big], -1, 255, cv2.FILLED)
    return out


def _is_closed(ink: np.ndarray) -> bool:
    """Does the main blob enclose paper (a fillable outline), or is it just
    a pen line with a gap in it?"""
    region = _outer_region(ink)
    return region is not None and (np.count_nonzero(region & ~ink)
                                   > 1.5 * np.count_nonzero(ink & region))


def _pick_ink(gray: np.ndarray, mode: str, invert: bool,
              warnings: list[str]) -> np.ndarray:
    """Choose the global or local ink mask (see ink_mask).

    outline -> local. filled -> global. auto -> global only when it looks
    like a real fill: mostly ink inside the drawn shape *and* not spilling
    far outside it (a cast shadow spills; a coloured-in shape doesn't)."""
    if mode == "filled":
        return _drop_border_junk(ink_mask(gray, invert), warnings)
    loc = _drop_border_junk(ink_mask(gray, invert, local=True), [])
    if mode == "outline":
        return loc
    glob = _drop_border_junk(ink_mask(gray, invert), [])
    region = _outer_region(loc)
    if region is None or not np.count_nonzero(glob):
        return glob if np.count_nonzero(glob) else loc
    area = max(1, np.count_nonzero(region))
    inside = np.count_nonzero(glob & region) / area
    spill = np.count_nonzero(glob & ~region) / area
    if inside >= OUTLINE_INK_RATIO and spill < 0.15:
        return _drop_border_junk(ink_mask(gray, invert), warnings)
    if spill >= 0.15:
        warnings.append("there's a shadow or dark patch on the page — traced "
                        "the pen lines through it; even light works best")
    return loc


def _close_gaps(ink: np.ndarray, stroke: float, warnings: list[str]) -> np.ndarray:
    """An outline with a small break in it can't be filled. Try bridging
    with growing closings (up to ~4 pen widths / 1.5% of the image) and keep
    the first one that encloses the shape."""
    lim = max(4 * stroke, 0.015 * max(ink.shape))
    k = max(7, int(2 * stroke) | 1)
    while k <= lim:
        bridged = cv2.morphologyEx(ink, cv2.MORPH_CLOSE,
                                   cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        if _is_closed(bridged):
            warnings.append(f"bridged a gap in the outline (~{k} px) — draw "
                            "the line all the way round to be sure")
            return bridged
        k = (k * 3 // 2) | 1
    return ink


def _contour_depths(hier: np.ndarray) -> list[int]:
    depths = []
    for i in range(len(hier)):
        d, p = 0, hier[i][3]
        while p != -1:
            d, p = d + 1, hier[p][3]
        depths.append(d)
    return depths


def _stroke_width(ink: np.ndarray) -> float:
    """Typical pen width in px: 2x the median distance-to-edge on the
    stroke's medial axis (approximated by local maxima of the DT)."""
    dt = cv2.distanceTransform(ink, cv2.DIST_L2, 5)
    ridge = (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8))) & (dt > 0.5)
    vals = dt[ridge]
    return float(2 * np.median(vals)) if vals.size else 1.0


def solid_mask(ink: np.ndarray, mode: str, warnings: list[str]):
    """Turn the ink mask into a solid silhouette. Returns (mask, mode, stroke)."""
    cnts, hier = cv2.findContours(ink, cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        raise ValueError("no drawing found in the image — is it blank, or "
                         "is the drawing lighter than the paper (try invert)?")
    hier = hier[0]
    depths = _contour_depths(hier)
    areas = [cv2.contourArea(c) for c in cnts]

    # Main shape = largest top-level contour; other top-level blobs are dropped.
    tops = [i for i, d in enumerate(depths) if d == 0]
    main = max(tops, key=lambda i: areas[i])
    others = [i for i in tops if i != main and areas[i] > NOISE_FRAC * areas[main]]
    if others:
        warnings.append(f"found {len(others) + 1} separate shapes; using the "
                        f"largest one only")

    # Collect main's descendants.
    keep = {main}
    for i in sorted(range(len(cnts)), key=lambda i: depths[i]):
        if hier[i][3] in keep and areas[i] > NOISE_FRAC * areas[main]:
            keep.add(i)

    filled_outer = np.zeros_like(ink)
    cv2.drawContours(filled_outer, cnts, main, 255, cv2.FILLED)
    ink_ratio = np.count_nonzero(ink & filled_outer) / max(1, np.count_nonzero(filled_outer))
    stroke = _stroke_width(ink & filled_outer)

    if mode == "auto":
        mode = "outline" if ink_ratio < OUTLINE_INK_RATIO else "filled"

    solid = np.zeros_like(ink)
    for i in sorted(keep, key=lambda i: depths[i]):
        d = depths[i]
        if mode == "filled":
            val = 255 if d % 2 == 0 else 0
        else:
            # Even-odd on pen lines: depth 0 = outer edge of the outline,
            # depth 3 = paper inside a line drawn inside it (a hole),
            # depth 4 = an island drawn inside that hole, ...
            if d % 4 == 0:
                val = 255
            elif d % 4 == 3:
                val = 0
            else:
                continue
        cv2.drawContours(solid, cnts, i, val, cv2.FILLED)

    if mode == "outline":
        # Pull every edge in from the line's outer side to its centre.
        r = stroke / 2
        if r >= 1:
            k = int(round(2 * r)) | 1
            solid = cv2.erode(solid, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
        # A drawn outline with a gap in it can't be filled — the "shape" is
        # just the pen line. Catch that before it turns into a wire model.
        if np.count_nonzero(solid) < 1.5 * np.count_nonzero(ink & filled_outer):
            warnings.append("the outline doesn't look closed — there may be a "
                            "gap in the line; the model is just the pen stroke")
    return solid, mode, stroke


def trace_sketch(path: str, mode: str = "auto", invert: bool = False,
          smooth: float = 2.0, holes: bool = True,
          simplify_px: float = 0.6) -> Trace:
    """Trace the main silhouette in a sketch image.

    mode        "auto" | "filled" | "outline"
    invert      drawing is lighter than its background
    smooth      edge smoothing (Gaussian sigma, px at WORK_PX resolution)
    holes       keep interior holes (False = fill them in)
    simplify_px max deviation when reducing the outline to fewer points
    """
    if mode not in ("auto", "filled", "outline"):
        raise ValueError(f"mode must be auto/filled/outline, not {mode!r}")
    warnings: list[str] = []
    gray = load_gray(path)
    ink = _pick_ink(gray, mode, invert, warnings)
    if not np.count_nonzero(ink):
        raise ValueError("no drawing found in the image — is it blank, or is "
                         "the drawing lighter than the paper (try invert)?")
    _, mode2, stroke = solid_mask(ink, mode, [])
    if mode2 == "outline" and not _is_closed(ink):
        ink = _close_gaps(ink, stroke, warnings)
    solid, mode, stroke = solid_mask(ink, mode, warnings)
    # Hand-drawn lines wobble by about a pen-width; smooth that away too.
    if mode == "outline" and smooth > 0:
        smooth = max(smooth, stroke / 2)
    solid = smooth_mask(solid, smooth)
    outer, hole_polys, final = mask_to_polys(solid, holes, simplify_px)
    return Trace(final, outer, hole_polys, mode, gray, warnings, stroke_px=stroke)
