"""Shared by both tracers (sketch.py, photo.py): image loading, the Trace
result type, mask -> polygon conversion and the trace check image.

Keep tracer-specific logic out of here — changes in this file affect both
bot options, and both test suites must pass after touching it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps

# Working resolution: long side of the image is resized to this many px.
# Big enough for detail, small enough that tracing stays well under a second.
WORK_PX = 1600
# Contours smaller than this fraction of the main shape's area are noise.
NOISE_FRAC = 0.002


@dataclass
class Trace:
    """A traced silhouette. `outer` and `holes` are polygons in pixel
    coordinates (x right, y down) of `work_image`."""
    mask: np.ndarray                    # uint8 0/255 solid silhouette
    outer: np.ndarray                   # (N,2) float
    holes: list[np.ndarray]             # each (M,2) float
    mode: str                           # how it was traced (for the report)
    work_image: np.ndarray              # grayscale image the mask came from
    warnings: list[str] = field(default_factory=list)
    stroke_px: float = 0.0              # sketch: estimated pen width
    mm_per_px: float | None = None      # photo: scale measured off the paper
    symmetry: str | None = None         # set by refine.prepare()

    @property
    def bbox(self):
        """(xmin, ymin, xmax, ymax) of the outer polygon in px."""
        xmin, ymin = self.outer.min(axis=0)
        xmax, ymax = self.outer.max(axis=0)
        return float(xmin), float(ymin), float(xmax), float(ymax)

    @property
    def size_mm(self):
        """(width, height) of the outline in mm, if the scale is known."""
        if not self.mm_per_px:
            return None
        x0, y0, x1, y1 = self.bbox
        return ((x1 - x0) * self.mm_per_px, (y1 - y0) * self.mm_per_px)


def load_bgr(path: str, work_px: int = WORK_PX) -> np.ndarray:
    """Load any image upright (EXIF applied) as BGR, long side = work_px.
    Transparency becomes white paper."""
    # Phone photos are stored sideways with an EXIF "rotate me" tag.
    im = ImageOps.exif_transpose(Image.open(path))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    img = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    s = work_px / max(h, w)
    if abs(s - 1) > 0.05:
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
        img = cv2.resize(img, (round(w * s), round(h * s)), interpolation=interp)
    return img


def load_gray(path: str) -> np.ndarray:
    return cv2.cvtColor(load_bgr(path), cv2.COLOR_BGR2GRAY)


def fit_surface(g: np.ndarray, sel: np.ndarray) -> np.ndarray:
    """Least-squares quadratic surface through g where sel is True — a
    smooth model of paper brightness (lighting gradient, soft shadows)."""
    h, w = g.shape
    yy, xx = np.mgrid[0:h:4, 0:w:4]
    s = sel[::4, ::4]
    if s.sum() < 50:
        return np.full(g.shape, max(1.0, float(g.max())), np.float32)
    x, y = xx[s] / w, yy[s] / h
    A = np.column_stack([np.ones_like(x), x, y, x * x, x * y, y * y])
    coef, *_ = np.linalg.lstsq(A, g[::4, ::4][s].astype(np.float64), rcond=None)
    Y, X = np.mgrid[0:h, 0:w]
    X, Y = X / w, Y / h
    return (coef[0] + coef[1] * X + coef[2] * Y + coef[3] * X * X
            + coef[4] * X * Y + coef[5] * Y * Y).astype(np.float32)


def smooth_mask(mask: np.ndarray, sigma: float) -> np.ndarray:
    """Blur + re-threshold: rounds off pixel staircase and edge wobble
    without moving the edge on average."""
    if sigma <= 0:
        return mask
    b = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), sigma)
    return np.where(b >= 127.5, 255, 0).astype(np.uint8)


def mask_to_polys(solid: np.ndarray, holes: bool = True,
                  simplify_px: float = 0.6):
    """Largest outline in a solid mask + its holes, as simplified polygons.
    Returns (outer, holes, final_mask) where final_mask is re-rendered from
    exactly the polygons that will be modelled."""
    cnts, hier = cv2.findContours(solid, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        raise ValueError("the traced shape vanished after cleanup — the "
                         "image may be too faint or the shape too thin")
    hier = hier[0]
    tops = [i for i in range(len(cnts)) if hier[i][3] == -1]
    main = max(tops, key=lambda i: cv2.contourArea(cnts[i]))
    min_area = NOISE_FRAC * cv2.contourArea(cnts[main])

    def poly(c):
        return cv2.approxPolyDP(c, simplify_px, True).reshape(-1, 2).astype(float)

    outer = poly(cnts[main])
    hole_polys = []
    if holes:
        hole_polys = [poly(cnts[i]) for i in range(len(cnts))
                      if hier[i][3] == main and cv2.contourArea(cnts[i]) > min_area]
    final = np.zeros_like(solid)
    cv2.fillPoly(final, [outer.astype(np.int32)], 255)
    for hp in hole_polys:
        cv2.fillPoly(final, [hp.astype(np.int32)], 0)
    return outer, hole_polys, final


def overlay(t: Trace, path: str) -> None:
    """Save the image with the traced outline drawn over it (green = outer,
    red = holes) so the user can check the trace before printing."""
    img = cv2.cvtColor(t.work_image, cv2.COLOR_GRAY2BGR)
    img = (img * 0.6 + 100).astype(np.uint8)          # wash out the image
    tint = img.copy()
    tint[t.mask > 0] = (200, 230, 200)
    img = cv2.addWeighted(img, 0.5, tint, 0.5, 0)
    lw = max(2, round(max(img.shape) / 400))
    cv2.polylines(img, [t.outer.astype(np.int32)], True, (40, 160, 40), lw, cv2.LINE_AA)
    for hp in t.holes:
        cv2.polylines(img, [hp.astype(np.int32)], True, (40, 40, 220), lw, cv2.LINE_AA)
    cv2.imwrite(path, img)
