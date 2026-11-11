"""Bot option 2 — PHOTOS: a real, flat-ish object photographed from above,
lying on a sheet of paper -> silhouette with its through-holes.

Separate from sketch.py on purpose: a photo of an object breaks the
assumptions a drawing tracer relies on (holes look dark or shaded rather
than paper-white, there's glare and texture, the object casts shadows), and
tuning for one must not regress the other.

Steps
1. Find the sheet of paper. If it shows as a clean 4-corner shape, warp the
   photo to look straight down at it and measure scale (mm/px) from the
   known paper size. Otherwise carry on unwarped and say so.
2. Separate object from paper by colour distance from the paper (fitted for
   lighting), Otsu threshold, largest blob.
3. Holes: regions inside the object fenced off by a closed edge that are
   clearly darker (looking down a hole) or lighter (paper showing through)
   than the object's own surface, roughly convex, and clear of its outer
   edge. See find_holes().
"""
from __future__ import annotations

import cv2
import numpy as np

from .common import NOISE_FRAC, Trace, fit_surface, load_bgr, mask_to_polys, smooth_mask

PAPER_MM = {"letter": (215.9, 279.4), "a4": (210.0, 297.0)}
# Surface brightness ratio beyond which a region inside the object is a hole.
DARK_RATIO = 0.55
LIGHT_RATIO = 1.30
# Holes must be roughly round/oval/slot-like, not streaks.
MIN_HOLE_SOLIDITY = 0.75
MIN_HOLE_PX = 60
# Narrow AND elongated = glare streak / scratch, not a slot. Narrow is
# relative to the object (sqrt of its area); round holes of any size pass.
THIN_FRAC = 0.04
STREAK_ASPECT = 2.5


def _order_quad(q: np.ndarray) -> np.ndarray:
    """Corners as top-left, top-right, bottom-right, bottom-left."""
    s, d = q.sum(1), np.diff(q, axis=1).ravel()
    return np.array([q[s.argmin()], q[d.argmin()], q[s.argmax()], q[d.argmax()]],
                    np.float32)


def find_paper(img: np.ndarray):
    """Return (paper_region_mask, quad or None). The region is the bright,
    colourless area around the object, holes filled."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    v, s = hsv[..., 2], hsv[..., 1]
    _, bright = cv2.threshold(cv2.GaussianBlur(v, (5, 5), 0), 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    paper = bright & np.where(s < 50, 255, 0).astype(np.uint8)
    paper = cv2.morphologyEx(paper, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    cnts, _ = cv2.findContours(paper, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.15 * img.shape[0] * img.shape[1]:
        return None, None
    region = np.zeros(img.shape[:2], np.uint8)
    cv2.drawContours(region, [c], -1, 255, cv2.FILLED)

    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    for f in np.linspace(0.005, 0.08, 40):
        q = cv2.approxPolyDP(hull, f * peri, True)
        if len(q) <= 4:
            break
    if len(q) != 4:
        return region, None
    q = q.reshape(4, 2).astype(np.float32)
    # Accept only if the paper really is that quadrilateral (two overlapping
    # sheets, a folded corner or a sheet running off-frame won't be).
    qa = cv2.contourArea(q)
    if cv2.contourArea(c) / qa < 0.93 or cv2.contourArea(hull) / qa < 0.96:
        return region, None
    h, w = img.shape[:2]
    if (q[:, 0] < 3).any() or (q[:, 1] < 3).any() or \
       (q[:, 0] > w - 4).any() or (q[:, 1] > h - 4).any():
        return region, None                     # sheet runs off the photo
    return region, _order_quad(q)


def rectify(img, quad, paper_mm, px_per_mm):
    """Warp so the paper is a flat rectangle at px_per_mm."""
    tl, tr, br, bl = quad
    wide = np.linalg.norm(tr - tl) + np.linalg.norm(br - bl) > \
        np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)
    pw, ph = sorted(paper_mm)
    if wide:
        pw, ph = ph, pw
    W, H = round(pw * px_per_mm), round(ph * px_per_mm)
    M = cv2.getPerspectiveTransform(quad, np.float32([[0, 0], [W, 0], [W, H], [0, H]]))
    return cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def object_mask(img: np.ndarray, region: np.ndarray | None, warnings: list[str]):
    """Object vs paper. Returns (filled outer silhouette, raw object mask)."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    if region is None:
        region = np.full(L.shape, 255, np.uint8)
    # Paper colour model from a first rough split, then refined.
    rough = np.zeros(L.shape, bool)
    for _ in range(2):
        paper = (region > 0) & ~rough
        Lp = fit_surface(L, paper)
        ap, bp = np.median(a[paper]), np.median(b[paper])
        dist = np.sqrt(np.clip(Lp - L, 0, None) ** 2 + 0.15 * np.clip(L - Lp, 0, None) ** 2
                       + (a - ap) ** 2 + (b - bp) ** 2)
        d8 = np.clip(dist, 0, 255).astype(np.uint8)
        d8 = cv2.GaussianBlur(d8, (5, 5), 0)
        inside = d8[region > 0]
        t, _ = cv2.threshold(inside.reshape(-1, 1), 0, 255,
                             cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        t = max(t, 30)                      # needs real contrast, not paper grain
        rough = (d8 > t) & (region > 0)
    obj = rough.astype(np.uint8) * 255
    obj = cv2.morphologyEx(obj, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    n, lab_, stats, _ = cv2.connectedComponentsWithStats(obj, connectivity=8)
    if n <= 1:
        raise ValueError("couldn't find an object on the paper — is it the "
                         "same colour as the paper?")
    h, w = obj.shape
    order = np.argsort(-stats[1:, cv2.CC_STAT_AREA]) + 1
    main = order[0]
    x, y, bw, bh, _ = stats[main]
    if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
        warnings.append("the object touches the edge of the photo/paper — "
                        "part of it may be cut off")
    if stats[main, cv2.CC_STAT_AREA] < 0.002 * h * w:
        raise ValueError("couldn't find an object on the paper — it's tiny or "
                         "very faint")
    big_others = [i for i in order[1:] if stats[i, cv2.CC_STAT_AREA]
                  > 0.05 * stats[main, cv2.CC_STAT_AREA]]
    if big_others:
        warnings.append(f"found {len(big_others) + 1} separate objects; "
                        f"using the largest one only")
    blob = np.where(lab_ == main, 255, 0).astype(np.uint8)
    cnts, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    filled = np.zeros_like(blob)
    cv2.drawContours(filled, cnts, -1, 255, cv2.FILLED)
    return filled, blob, L


def find_holes(img: np.ndarray, L: np.ndarray, filled: np.ndarray,
               warnings: list[str]) -> np.ndarray:
    """Mask of through-holes inside the object.

    A hole has a crisp, closed edge where the top face drops away, but its
    inside is not uniformly dark or light (a slot can shade from a dark
    wall, through plastic-grey, to lit paper). So holes are found as regions
    fenced off from the object's surface by a closed edge, then kept only if
    they look like holes: clearly darker/lighter than the plastic around
    them, roughly convex, and clear of the object's outer edge (side faces,
    edge glare). Scratches and scuffs don't form closed fences that size.
    """
    size = np.sqrt(np.count_nonzero(filled))
    band = max(3, int(size * 0.02))
    core = cv2.erode(filled, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * band + 1, 2 * band + 1)))

    # Brightness relative to the object's own (fitted) surface lighting.
    sel = core > 0
    for _ in range(3):
        S = np.maximum(fit_surface(L, sel), 1)
        r = L / S
        sel = (core > 0) & (r > DARK_RATIO) & (r < LIGHT_RATIO)
    odd = (r < DARK_RATIO) | (r > LIGHT_RATIO)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 9, 30, 7)
    med = float(np.median(gray[core > 0]))
    edges = cv2.Canny(gray, max(8, 0.2 * med), max(25, 0.6 * med))
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))       # close 1 px gaps

    free = ((core > 0) & (edges == 0)).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
    if n <= 1:
        return np.zeros_like(filled)
    surface = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    min_px = max(MIN_HOLE_PX, NOISE_FRAC * np.count_nonzero(filled))
    max_px = 0.3 * np.count_nonzero(filled)

    holes = np.zeros_like(filled)
    rejected = 0
    for i in range(1, n):
        if i == surface or stats[i, cv2.CC_STAT_AREA] < MIN_HOLE_PX / 4:
            continue
        # Region + its fence, with anything nested inside it filled in.
        blob = cv2.dilate(np.where(lab == i, 255, 0).astype(np.uint8),
                          np.ones((5, 5), np.uint8))
        cnts, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        c = max(cnts, key=cv2.contourArea)
        area = cv2.contourArea(c)
        if area < min_px:
            continue                                # speckle, not worth a word
        blob[:] = 0
        cv2.drawContours(blob, [c], -1, 255, cv2.FILLED)
        inside = blob > 0
        hull = cv2.convexHull(c)
        (_, _), (rw, rh), _ = cv2.minAreaRect(c)
        thin, long_ = min(rw, rh), max(rw, rh)
        streak = thin < THIN_FRAC * size and long_ > STREAK_ASPECT * thin
        if (area > max_px or streak
                or np.count_nonzero(inside & (core == 0)) > 0.02 * area
                or area / max(cv2.contourArea(hull), 1) < MIN_HOLE_SOLIDITY
                or odd[inside].mean() < 0.25):
            rejected += 1
            continue
        # Through-holes are almost always convex (round, slot, square);
        # the hull also fills bites taken by glare on a hole's wall.
        cv2.drawContours(holes, [hull], -1, 255, cv2.FILLED)
    if rejected:
        warnings.append(f"ignored {rejected} enclosed patch{'es' * (rejected > 1)} "
                        f"inside the object that didn't look like holes "
                        f"(shadow, glare, scuff or a side face) — check trace.png")
    return holes


def trace_photo(path: str, paper: str = "letter", holes: bool = True,
                smooth: float = 2.0, simplify_px: float = 0.6) -> Trace:
    """Trace a real object lying on paper, photographed from above.

    paper   "letter" | "a4" | "none" — sheet size used to straighten the
            photo and measure the object
    holes   detect through-holes (False = solid outline only)
    """
    if paper not in (*PAPER_MM, "none"):
        raise ValueError(f"paper must be letter/a4/none, not {paper!r}")
    warnings: list[str] = []
    img = load_bgr(path)
    mm_per_px = None
    region, quad = find_paper(img)
    if paper != "none" and quad is not None:
        px_per_mm = 1600 / max(PAPER_MM[paper])
        img = rectify(img, quad, PAPER_MM[paper], px_per_mm)
        mm_per_px = 1 / px_per_mm
        region = None                               # whole image is paper now
        mode = f"photo, straightened on {paper} paper"
    else:
        mode = "photo"
        if paper != "none":
            warnings.append("couldn't find all 4 corners of the paper, so the "
                            "photo wasn't straightened — shoot straight down "
                            "with the whole sheet in frame for a truer shape")
        if region is None:
            warnings.append("no paper found behind the object; tracing the "
                            "whole photo")
    filled, _, L = object_mask(img, region, warnings)
    solid = filled.copy()
    if holes:
        solid[find_holes(img, L, filled, warnings) > 0] = 0
    solid = smooth_mask(solid, smooth)
    outer, hole_polys, final = mask_to_polys(solid, holes, simplify_px)
    if hole_polys:
        warnings.append(f"found {len(hole_polys)} hole{'s' * (len(hole_polys) > 1)}; "
                        f"hole sizes from a photo are ±1 mm — check trace.png and "
                        f"adjust in the .scad if they must fit something")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return Trace(final, outer, hole_polys, mode, gray, warnings, mm_per_px=mm_per_px)
