"""Shared clean-up for both options (sketch + photo): symmetry, straightening,
labelled edges and label-driven edits.

Runs after a tracer, before the model. Two stages:

1. prepare(trace, symmetry, straighten)  — pixel stage, on the mask
   * squares the shape up (rotates so its straight edges run level) when
     straightening or symmetry is asked for
   * symmetry: finds the mirror line, averages the two sides
2. Shape (in real mm, via model.build_model's refiner hook)
   * splits every outline into labelled features: E# edges, C# corners
     (sharp, or rounded with R + centre), H# holes. Mirror-image partners
     share one label; only the kept half (right/top) is labelled.
   * straighten: straight edges -> best-fit line, snapped level/plumb when
     within SNAP_DEG, clean corners; round holes -> true circles
   * edits from the user, by label: "E3 up 11", "E4 out 1", "E3 straight",
     "E1 level", "C2 R5", "C4 sharp", "H1 circle 6", "H1 left 3", "H2 remove"
     — applied in the final frame so typed numbers come out exact
   * symmetry made exact by mirroring one half onto the other
   * legend(): exact geometry per label for drawing.py's feature map

Changes here affect both bot options — run the full test suite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np
from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import polylabel, unary_union

from . import fasteners as fx
from .common import Trace, mask_to_polys

SYMMETRY = (None, "lr", "tb", "both")
SNAP_DEG = 5.0            # straight edges this close to level/plumb get snapped
DESKEW_MAX_DEG = 20.0     # never "square up" by more than this
BOW_MAX = 0.02            # sagitta/length above this = a curve, not a line
STEP_TURN = 30.0          # deg: a curve between lines turning less = a step, not a corner
CIRCLE_RMS = 0.04         # hole residual/radius below this = a round hole
SYM_WARN = 0.92           # mirror match (IoU) below this gets a warning
MIN_EDGE = 0.8            # mm: an edge (or hole wall) thinner than this won't print

# Edit vocabulary (what the user can reply with, by label letter).
EDGE_ACTIONS = {
    "straight": "straight", "line": "straight", "flat": "straight",
    "arc": "arc", "curve": "arc", "curved": "arc",
    "raw": "raw", "keep": "raw", "asdrawn": "raw", "as-drawn": "raw",
    "level": "h", "h": "h", "horizontal": "h",
    "plumb": "v", "v": "v", "vertical": "v",
    "auto": "auto", "reset": "auto",
}
CORNER_ACTIONS = {
    "sharp": "sharp", "square": "sharp", "pointed": "sharp",
    "radius": "radius", "r": "radius", "round": "radius", "fillet": "radius",
    "rounded": "radius",
    "straight": "straight", "flat": "straight", "chamfer": "straight",
    "arc": "arc", "raw": "raw", "keep": "raw", "auto": "auto", "reset": "auto",
}
HOLE_ACTIONS = {
    "circle": "circle", "round": "circle",
    "remove": "remove", "delete": "remove", "fill": "remove", "none": "remove",
    "raw": "raw", "keep": "raw", "asdrawn": "raw",
    "auto": "auto", "reset": "auto",
}
MOVES = {"up": (0, 1), "north": (0, 1), "down": (0, -1), "south": (0, -1),
         "left": (-1, 0), "west": (-1, 0), "right": (1, 0), "east": (1, 0),
         "out": "out", "outward": "out", "outwards": "out",
         "in": "in", "inward": "in", "inwards": "in"}
ACTION_NAMES = {"h": "level", "v": "plumb"}
FILLER = {"to", "make", "it", "a", "an", "be", "is", "dia", "d", "=", "needs", "need",
          "move", "moved", "moves", "by", "should", "please", "shift", "nudge",
          "the", "of", "with", "mm", "edge", "corner", "hole", "and", "go", "goes"}


# ---------------------------------------------------------------- pixel stage

def _rotate(img, angle_deg, interp, border=0):
    """Rotate about the centre, growing the canvas so nothing is cut off."""
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle_deg, 1.0)
    c, s = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * s + w * c) + 2, int(h * c + w * s) + 2
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, M, (nw, nh), flags=interp,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=border)


def _dense_rings(mask):
    """Outer + hole contours at full pixel density, lightly smoothed."""
    cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    hier = hier[0]
    tops = [i for i in range(len(cnts)) if hier[i][3] == -1]
    main = max(tops, key=lambda i: cv2.contourArea(cnts[i]))
    min_a = 0.002 * cv2.contourArea(cnts[main])
    rings = [cnts[main]] + [cnts[i] for i in range(len(cnts))
                            if hier[i][3] == main and cv2.contourArea(cnts[i]) > min_a]
    out = []
    for c in rings:
        p = c.reshape(-1, 2).astype(float)
        if len(p) >= 9:                       # circular blur along the contour
            k = np.exp(-0.5 * (np.arange(-4, 5) / 1.5) ** 2)
            k /= k.sum()
            p = np.column_stack([np.convolve(np.r_[p[-4:, i], p[:, i], p[:4, i]], k, "valid")
                                 for i in range(2)])
        out.append(p)
    return out[0], out[1:]


def _line_angles(pts, tol):
    """(angle_deg, length) of the straight runs in a contour."""
    out = []
    for seg in _split(pts, tol, closed=True):
        if seg[0] == "line":
            d = seg[1][-1] - seg[1][0]
            out.append((np.degrees(np.arctan2(d[1], d[0])), np.hypot(*d)))
    return out


def _deskew_angle(mask) -> float | None:
    """Angle that brings the dominant straight edges level/plumb."""
    outer, holes = _dense_rings(mask)
    size = np.sqrt(cv2.contourArea(outer.astype(np.float32)))
    votes = []
    for ring in [outer, *holes]:
        votes += _line_angles(ring, max(1.5, 0.006 * size))
    if not votes:
        return None
    a = np.array([((v + 45) % 90) - 45 for v, _ in votes])     # fold to ±45°
    wts = np.array([w for _, w in votes])
    bins = np.arange(-45, 45.25, 0.25)
    hist = np.zeros(len(bins))
    for ai, wi in zip(a, wts):
        hist += wi * np.exp(-0.5 * ((bins - ai) / 1.0) ** 2)
    best = bins[hist.argmax()]
    near = np.abs(a - best) < 2
    best = float(np.average(a[near], weights=wts[near]))
    if abs(best) > DESKEW_MAX_DEG or wts[near].sum() < 0.25 * wts.sum():
        return None
    return best


def _mirror(mask, axis, c):
    h, w = mask.shape
    M = np.float32([[-1, 0, 2 * c], [0, 1, 0]]) if axis == "x" else \
        np.float32([[1, 0, 0], [0, -1, 2 * c]])
    return cv2.warpAffine(mask, M, (w, h), flags=cv2.INTER_LINEAR)


def _iou(a, b):
    a, b = a > 127, b > 127
    return np.count_nonzero(a & b) / max(1, np.count_nonzero(a | b))


def _best_mirror(mask, axis):
    """Mirror line position (px) maximising overlap, + that overlap."""
    s = 400 / max(mask.shape)
    small = cv2.resize(mask, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    m = cv2.moments(small, True)
    c0 = m["m10"] / m["m00"] if axis == "x" else m["m01"] / m["m00"]
    span = 0.06 * (small.shape[1] if axis == "x" else small.shape[0])
    best = max(np.arange(c0 - span, c0 + span, 0.25),
               key=lambda c: _iou(small, _mirror(small, axis, c)))
    return best / s, _iou(small, _mirror(small, axis, best))


def _sym_angle(mask, axis) -> float:
    """For shapes with no straight edges: the tilt that best shows the
    mirror symmetry."""
    s = 300 / max(mask.shape)
    small = cv2.resize(mask, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)

    def score(a):
        r = _rotate(small, a, cv2.INTER_LINEAR)
        return _best_mirror(r, axis)[1]
    coarse = max(np.arange(-15, 15.1, 1.5), key=score)
    return float(max(np.arange(coarse - 1.5, coarse + 1.6, 0.25), key=score))


def prepare(t: Trace, symmetry: str | None, straighten: bool) -> Trace:
    """Pixel stage: square up + symmetrise the mask. Returns a new Trace
    (mask, outline and image all in the same, possibly rotated, frame)."""
    if symmetry not in SYMMETRY:
        raise ValueError(f"symmetry must be one of lr / tb / both, not {symmetry!r}")
    if not symmetry and not straighten:
        return t
    warnings = list(t.warnings)
    mask, gray = t.mask, t.work_image
    axes = {"lr": ["x"], "tb": ["y"], "both": ["x", "y"]}.get(symmetry, [])

    angle = _deskew_angle(mask)
    if angle is None and axes:
        angle = _sym_angle(mask, axes[0])
    if angle is not None and abs(angle) > 0.2:
        # cv2 angles are counter-clockwise in y-down image space
        mask = _rotate(mask, angle, cv2.INTER_LINEAR)
        mask = np.where(mask > 127, 255, 0).astype(np.uint8)
        gray = _rotate(gray, angle, cv2.INTER_LINEAR, border=int(np.median(gray)))
        warnings.append(f"squared the shape up by rotating it {abs(angle):.1f}°")

    sym_lines = {}
    for ax in axes:
        c, iou = _best_mirror(mask, ax)
        name = "left/right" if ax == "x" else "top/bottom"
        if iou < SYM_WARN:
            warnings.append(f"the shape is only {iou:.0%} {name} symmetric as "
                            f"traced — made symmetric anyway; check drawing.png")
        soft = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 1.0)
        mirrored = _mirror(soft, ax, c)
        mask = np.where((soft + mirrored) / 2 >= 127.5, 255, 0).astype(np.uint8)
        sym_lines[ax] = c

    outer, holes, final = mask_to_polys(mask, True, 0.6)
    out = Trace(final, outer, holes, t.mode, gray, warnings,
                stroke_px=t.stroke_px, mm_per_px=t.mm_per_px)
    out.symmetry = symmetry
    return out


# ------------------------------------------------------------------- mm stage

def _bow(pts):
    """Sagitta / length of a run of points (0 for a perfect line)."""
    a, b = pts[0], pts[-1]
    L = np.hypot(*(b - a))
    if L < 1e-9:
        return 1.0
    d = (b - a) / L
    n = np.array([-d[1], d[0]])
    t = (pts - a) @ d / L
    r = (pts - a) @ n
    # fit r = k*t*(1-t): the bow of an arc through both ends
    basis = t * (1 - t)
    k = (basis @ r) / max(basis @ basis, 1e-12)
    return abs(k) / 4 / L


def _line_ok(run, tol):
    """Straight enough to be one line: small residual and no bow."""
    L = np.hypot(*(run[-1] - run[0]))
    if L < 1e-9 or len(run) < 3:
        return False
    d = (run[-1] - run[0]) / L
    r = (run - run[0]) @ np.array([-d[1], d[0]])
    rms = np.sqrt(np.mean((r - r.mean()) ** 2))
    # bow allowed up to the wobble tolerance, so a shaky hand-drawn line
    # still counts; a real arc bows far more than that
    return rms <= max(tol, 0.012 * L) and _bow(run) * L <= max(tol, BOW_MAX * L)


def _corners(pts, k, min_deg=30.0):
    """Indices of sharp turns, measured over +-k points (scale-aware, so
    hand wobble and blur don't count as corners)."""
    n = len(pts)
    a = pts - np.roll(pts, k, axis=0)
    b = np.roll(pts, -k, axis=0) - pts
    ang = np.degrees(np.abs(np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0],
                                       (a * b).sum(1))))
    out = []
    for i in range(n):
        if ang[i] >= min_deg:
            win = ang[np.arange(i - k, i + k + 1) % n]
            if ang[i] >= win.max() and (not out or i - out[-1] > k):
                out.append(i)
    if len(out) > 1 and out[0] + n - out[-1] <= k:
        out.pop() if ang[out[-1]] < ang[out[0]] else out.pop(0)
    return out


def _peel(run, tol, min_len, step, depth=0):
    """Split a non-straight run into curve / line / curve pieces and recurse.

    The straight part is found three ways and the longest that passes
    _line_ok wins: grown from the run's start, grown from its end (a side
    running into a round top), or the flattest stretch in the middle (a side
    of a rounded rectangle runs mid-corner to mid-corner, so it starts and
    ends with half an arc)."""
    m = len(run)
    w = max(3, step)
    if _line_ok(run, tol) and np.hypot(*(run[-1] - run[0])) >= min_len:
        return [("line", run)]
    if m < 2 * w + 3 or depth > 6:
        return [("curve", run)]

    def ok(i, j):
        c = run[i:j + 1]
        return len(c) >= 3 and np.hypot(*(c[-1] - c[0])) >= min_len and _line_ok(c, tol)

    cands = []
    inc = max(2, w // 3)
    best = None                                  # grow from the start
    for j in range(2 * w, m, inc):
        if ok(0, j):
            best = j
        elif best is not None:
            break
    if best is not None:
        cands.append((0, best))
    best = None                                  # grow from the end
    for i in range(m - 1 - 2 * w, -1, -inc):
        if ok(i, m - 1):
            best = i
        elif best is not None:
            break
    if best is not None:
        cands.append((best, m - 1))
    a = run[w:m - w] - run[:m - 2 * w]           # flattest stretch
    b = run[2 * w:] - run[w:m - w]
    turn = np.degrees(np.abs(np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0],
                                        (a * b).sum(1))))
    flat = np.r_[np.full(w, turn[0] < 8), turn < 8, np.full(w, turn[-1] < 8)]
    span, cur, start = (0, 0, 0), 0, 0
    for i, f in enumerate(flat):
        if f:
            start = i if cur == 0 else start
            cur += 1
            if cur > span[0]:
                span = (cur, start, i)
        else:
            cur = 0
    _, s0, e0 = span
    for _ in range(5):                           # it can bleed into an arc: trim
        if e0 - s0 < 3:
            break
        if ok(s0, e0):
            cands.append((s0, e0))
            break
        s0, e0 = s0 + max(1, w // 2), e0 - max(1, w // 2)
    if not cands:
        return [("curve", run)]
    s0, e0 = max(cands, key=lambda c: np.hypot(*(run[c[1]] - run[c[0]])))
    if s0 <= 1 and e0 >= m - 2:
        return [("line", run)]
    out = []
    if s0 > 1:
        out += _peel(run[:s0 + 1], tol, min_len, step, depth + 1)
    out.append(("line", run[(0 if s0 <= 1 else s0):(m if e0 >= m - 2 else e0 + 1)]))
    if e0 < m - 2:
        out += _peel(run[e0:], tol, min_len, step, depth + 1)
    return out


def _split(pts, tol, closed=True, min_len=None):
    """Split a dense closed contour into ("line"|"curve", points) runs."""
    n = len(pts)
    seg_len = np.hypot(*np.diff(np.vstack([pts, pts[:1]]), axis=0).T)
    per = seg_len.sum()
    if min_len is None:
        min_len = max(6 * tol, 0.04 * per)
    k = max(4, int(0.015 * n))
    cs = _corners(pts, k)
    if not cs:
        if _line_ok(pts, tol):
            return [("line", pts)]
        return [("curve", pts)]
    pts = np.roll(pts, -cs[0], axis=0)
    cs = [(c - cs[0]) % n for c in cs] + [n]
    runs = []
    for a, b in zip(cs[:-1], cs[1:]):
        run = pts[a:b + 1] if b < n else np.vstack([pts[a:], pts[:1]])
        if _line_ok(run, tol) and np.hypot(*(run[-1] - run[0])) >= min_len:
            runs.append(["line", run])
        else:
            runs += [list(r) for r in _peel(run, tol, min_len, k)]
    merged = [runs[0]]
    for kind, run in runs[1:]:
        pk, prun = merged[-1]
        if kind == pk == "curve":
            merged[-1][1] = np.vstack([prun, run[1:]])
        elif kind == pk == "line" and _line_ok(np.vstack([prun, run[1:]]), tol):
            merged[-1][1] = np.vstack([prun, run[1:]])
        else:
            merged.append([kind, run])
    if len(merged) > 1 and merged[0][0] == merged[-1][0] == "curve":
        merged[0][1] = np.vstack([merged[-1][1], merged[0][1][1:]])
        merged.pop()
    return [(k_, r) for k_, r in merged]


def _fit_line(pts):
    vx, vy, x0, y0 = cv2.fitLine(pts.astype(np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
    return np.array([x0, y0], float), np.array([vx, vy], float)


def _fit_circle(pts):
    x, y = pts[:, 0], pts[:, 1]
    A = np.column_stack([x, y, np.ones_like(x)])
    sol, *_ = np.linalg.lstsq(A, x * x + y * y, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    r = np.sqrt(max(sol[2] + cx * cx + cy * cy, 1e-12))
    rms = np.sqrt(np.mean((np.hypot(x - cx, y - cy) - r) ** 2))
    return np.array([cx, cy]), r, rms


def _fit_ellipse(pts):
    """cv2 ellipse if it fits the ring well (rms radial error < CIRCLE_RMS),
    else None."""
    if len(pts) < 6:
        return None
    (cx, cy), (a1, a2), ang = cv2.fitEllipse(pts.astype(np.float32))
    if min(a1, a2) <= 0:
        return None
    th = np.radians(ang)
    q = (pts - [cx, cy]) @ np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    rr = np.hypot(q[:, 0] / (a1 / 2), q[:, 1] / (a2 / 2))
    if np.sqrt(np.mean((rr - 1) ** 2)) > CIRCLE_RMS:
        return None
    return (cx, cy), (a1, a2), ang


def _project(p, line):
    o, d = line
    return o + ((p - o) @ d) * d


def _intersect(l1, l2):
    (o1, d1), (o2, d2) = l1, l2
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < np.sin(np.radians(10)):
        return None
    t = ((o2 - o1)[0] * d2[1] - (o2 - o1)[1] * d2[0]) / den
    return o1 + t * d1


def _smooth_open(pts, frac=0.04):
    """Gaussian-smooth an open run along its length, ends pinned."""
    n = len(pts)
    sig = max(2.0, frac * n)
    if n < 7:
        return pts
    h = int(3 * sig)
    k = np.exp(-0.5 * (np.arange(-h, h + 1) / sig) ** 2)
    k /= k.sum()
    pad = np.vstack([np.repeat(pts[:1], h, 0), pts, np.repeat(pts[-1:], h, 0)])
    out = np.column_stack([np.convolve(pad[:, i], k, "valid") for i in range(2)])
    w = np.minimum(np.arange(n), np.arange(n)[::-1]) / max(h, 1)
    w = np.clip(w, 0, 1)[:, None]                  # ease back to the pinned ends
    return w * out + (1 - w) * pts


def _simplify(pts, tol):
    if len(pts) < 3:
        return pts
    return cv2.approxPolyDP(pts.astype(np.float32).reshape(-1, 1, 2), tol, False) \
        .reshape(-1, 2).astype(float)


def _orient(line, pts):
    """Point a fitted line's direction the way the run is traversed."""
    o, d = line
    return (o, d if d @ (pts[-1] - pts[0]) >= 0 else -d)


def _fillet(la, lb, P, r, max_t):
    """Arc of radius r tangent to incoming line la and outgoing line lb that
    meet at P. Returns (points, centre, r_used) or None."""
    u, v = -la[1], lb[1]
    cphi = float(np.clip(u @ v, -1, 1))
    phi = np.arccos(cphi)
    if phi < np.radians(5) or phi > np.radians(175):
        return None
    t = r / np.tan(phi / 2)
    if t > max_t:
        t = max_t
        r = t * np.tan(phi / 2)
    T1, T2 = P + u * t, P + v * t
    w = (u + v) / np.hypot(*(u + v))
    C = P + w * r / np.sin(phi / 2)
    a0 = np.arctan2(*(T1 - C)[::-1])
    a1 = np.arctan2(*(T2 - C)[::-1])
    span = (a1 - a0 + np.pi) % (2 * np.pi) - np.pi
    k = max(6, int(abs(span) * r / 0.3))
    a = a0 + span * np.linspace(0, 1, k + 1)
    return list(C + r * np.column_stack([np.cos(a), np.sin(a)])), C, r


def _arc_len(p):
    return float(np.sum(np.hypot(*np.diff(p, axis=0).T))) if len(p) > 1 else 0.0


def _turn_deg(a, b) -> float:
    """How far the outline turns from segment a's direction to b's."""
    da, db = a.pts[-1] - a.pts[0], b.pts[-1] - b.pts[0]
    cos = da @ db / max(np.hypot(*da) * np.hypot(*db), 1e-12)
    return float(np.degrees(np.arccos(np.clip(cos, -1, 1))))


@dataclass
class Segment:
    """One run of the outline: E# edge, or C# when it is a rounded corner
    (a curve between two straight edges that fits an arc)."""
    kind: str                     # "line" | "curve" — as traced
    pts: np.ndarray               # dense, mm
    label: str = ""
    action: str = "auto"
    radius: float | None = None   # user R (rounded corners)
    move: np.ndarray = field(default_factory=lambda: np.zeros(2))
    offset: float = 0.0           # + = outward
    corner: bool = False
    auto_fit: tuple | None = None  # (centre, r) when an auto corner is re-fitted
    primary: bool = True
    ops: tuple = ()               # mirror ops taking the primary to this one
    out: np.ndarray | None = None
    geom: dict = field(default_factory=dict)

    @property
    def length(self):
        return _arc_len(self.pts)

    @property
    def mid(self):
        d = np.r_[0, np.cumsum(np.hypot(*np.diff(self.pts, axis=0).T))]
        i = int(np.searchsorted(d, d[-1] / 2))
        return self.pts[min(i, len(self.pts) - 1)]


@dataclass
class Joint:
    """A sharp corner where two straight edges meet: C#."""
    at: np.ndarray
    label: str = ""
    action: str = "auto"
    radius: float | None = None
    primary: bool = True
    ops: tuple = ()
    out: list | None = None
    geom: dict = field(default_factory=dict)


@dataclass
class Ring:
    segs: list[Segment]
    dense: np.ndarray
    hole: bool = False
    joints: list = field(default_factory=list)   # joints[i]: before segs[i]
    label: str = ""               # holes only
    action: str = "auto"
    dia: float | None = None      # user-set hole diameter, mm
    move: np.ndarray = field(default_factory=lambda: np.zeros(2))
    primary: bool = True
    ops: tuple = ()
    out: np.ndarray | None = None
    circle: tuple | None = None   # (centre, r) when made round
    ellipse: tuple | None = None  # (axis1, axis2) when made an oval
    geom: dict = field(default_factory=dict)
    fastener: dict | None = None  # {"m": "M3", "style": screw|nut|both, "face": top|bottom}
    slot: dict | None = None      # {"w": width, "l": overall length, "angle": deg}
    added: bool = False           # made by 'add hole', not traced

    @property
    def centre(self):
        return self.dense.mean(axis=0) + self.move


@dataclass
class Shape:
    """A traced outline in mm, split into labelled, editable features:
    E# edges, C# corners (sharp or radiused), H# holes."""
    outer: Ring
    holes: list[Ring]
    length: float
    width: float
    mirrors: list[str]            # model-frame mirror axes: "x" (about L/2), "y"
    tol: float
    warnings: list[str] = field(default_factory=list)
    resized: bool = False         # a move changed the overall size

    # ---- construction
    @classmethod
    def from_trace(cls, t: Trace, to_mm, rotated: bool, length, width):
        outer_px, holes_px = _dense_rings(t.mask)
        outer = to_mm(outer_px)
        holes = [to_mm(h) for h in holes_px]
        px_mm = max(length, width) / max(np.ptp(outer_px, axis=0))
        size = np.sqrt(length * width)
        tol = max(2.0 * px_mm, 0.006 * size)
        img_axes = {"lr": ["x"], "tb": ["y"], "both": ["x", "y"]}.get(t.symmetry, [])
        # image left/right mirror = model x mirror, unless the model turned 90°
        mirrors = [({"x": "y", "y": "x"}[a] if rotated else a) for a in img_axes]
        rings = [Ring([Segment(k, p) for k, p in _split(outer, tol)], outer)]
        rings += [Ring([Segment(k, p) for k, p in _split(h, tol)], h, hole=True)
                  for h in holes]
        sh = cls(rings[0], rings[1:], float(length), float(width), mirrors, tol)
        sh._find_corners(sh.outer)
        sh._label()
        return sh

    def _find_corners(self, ring):
        segs, n = ring.segs, len(ring.segs)
        ring.joints = [None] * n
        if n < 2:
            return
        for i, s in enumerate(segs):
            prev, nxt = segs[i - 1], segs[(i + 1) % n]
            if s.kind == "curve" and prev.kind == nxt.kind == "line" and n >= 3:
                if _turn_deg(prev, nxt) < STEP_TURN:
                    # Both neighbours run the same way: this is a short
                    # step / jog between them, not a rounded corner. It
                    # stays an edge of its own (a line if it's straight
                    # enough) so it gets an E label and can be edited —
                    # as a corner, straighten squared it to 90° (2026-10-28).
                    if _line_ok(s.pts, max(self.tol, 0.08 * np.hypot(*(s.pts[-1] - s.pts[0])))):
                        s.kind = "line"
                    continue
                c, r, rms = _fit_circle(s.pts)
                if rms < 1.5 * CIRCLE_RMS * r and r < 0.5 * max(self.length, self.width):
                    s.corner = True
            if prev.kind == "line" and s.kind == "line":
                ring.joints[i] = Joint(s.pts[0].copy())

    # ---- symmetry helpers
    def _mirror_pts(self, pts, ax):
        q = np.array(pts, float).copy()
        if ax == "x":
            q[..., 0] = self.length - q[..., 0]
        else:
            q[..., 1] = self.width - q[..., 1]
        return q

    def _ops(self):
        ops = [(a,) for a in self.mirrors]
        if len(self.mirrors) == 2:
            ops.append(tuple(self.mirrors))
        return ops

    def _apply_ops(self, pts, op):
        for a in op:
            pts = self._mirror_pts(pts, a)
        return pts

    def _mirror_vec(self, v, op):
        v = np.array(v, float).copy()
        for a in op:
            v[0 if a == "x" else 1] *= -1
        return v

    def _keep_score(self, p):
        """Higher = more inside the half that is kept/labelled (right, top)."""
        sc = 0.0
        if "x" in self.mirrors:
            sc += p[0] - self.length / 2
        if "y" in self.mirrors:
            sc += p[1] - self.width / 2
        return sc

    def _group_label(self, items, prefix, start=1):
        """items: (obj, anchor) in reading order. Groups mirror partners,
        labels primaries in order, sets primary/ops. Returns next number."""
        tol = max(3 * self.tol, 0.03 * np.sqrt(self.length * self.width))
        done = set()
        n = start
        groups = []
        for i, (o, p) in enumerate(items):
            if i in done:
                continue
            g = [(i, ())]
            done.add(i)
            for op in self._ops():
                m = self._apply_ops(p, op)
                for j, (o2, p2) in enumerate(items):
                    if j not in done and type(o2) is type(o) and \
                            getattr(o2, "corner", None) == getattr(o, "corner", None) and \
                            np.hypot(*(p2 - m)) < tol:
                        g.append((j, op))
                        done.add(j)
            groups.append(g)
        # primary = member deepest in the kept half; numbering follows the
        # primaries' reading order
        firsts = []
        for g in groups:
            pi, pop = max(g, key=lambda m: self._keep_score(items[m[0]][1]))
            firsts.append((pi, pop, g))
        for pi, pop, g in sorted(firsts, key=lambda f: f[0]):
            if self.mirrors and len(g) == 1 and self._keep_score(items[pi][1]) < -tol:
                # only on the discarded half, no partner: exact symmetry
                # replaces it with the kept side's mirror, so it never exists
                items[pi][0].label = ""
                continue
            lab = f"{prefix}{n}"
            n += 1
            for j, op in g:
                o = items[j][0]
                o.label = lab
                o.primary = j == pi
                # op taking the primary to member j: undo primary's, apply j's
                o.ops = tuple(sorted(set(op) ^ set(pop)))
        return n

    def _label(self):
        """Reading order: clockwise from the top-left, like a drawing."""
        ring = self.outer
        segs, n = ring.segs, len(ring.segs)
        elems = []                             # (obj, anchor) along the ring
        for i in range(n):
            if ring.joints[i] is not None:
                elems.append((ring.joints[i], ring.joints[i].at))
            elems.append((segs[i], segs[i].mid))
        if _signed_area(ring.dense) > 0:       # ccw in y-up: walk it backwards
            elems = elems[::-1]
        k = int(np.argmin([p[0] - p[1] for _, p in elems]))
        elems = elems[k:] + elems[:k]
        edges = [(o, p) for o, p in elems if isinstance(o, Segment) and not o.corner]
        corners = [(o, p) for o, p in elems if isinstance(o, Joint) or
                   (isinstance(o, Segment) and o.corner)]
        self._group_label(edges, "E")
        self._group_label(corners, "C")
        hs = sorted(self.holes, key=lambda h: (-round(h.centre[1], 1), h.centre[0]))
        self._group_label([(h, h.centre) for h in hs], "H")

    @property
    def labels(self) -> list[str]:
        seen = []
        for o in self._objects():
            if o.label not in seen:
                seen.append(o.label)
        return sorted(seen, key=lambda x: ("ECH".index(x[0]), int(x[1:])))

    def _objects(self):
        out = list(self.outer.segs) + [j for j in self.outer.joints if j is not None]
        return [o for o in out + list(self.holes) if o.label]

    # ---- edits
    def apply_edits(self, edits):
        """edits: [(label, action, value)], applied in order. Shape actions
        replace earlier ones for that label; moves add up."""
        if isinstance(edits, dict):                     # older job.json
            edits = [(k, *v) for k, v in edits.items()]
        known = set(self.labels)
        for label, action, value in edits:
            if label in ("TOP", "BOTTOM"):
                continue                 # edge finish: handled by the model
            if label == "ADD":
                known.add(self._add_hole(value))
                continue
            if label not in known:
                raise ValueError(f"there's no {label} on this part — labels are "
                                 f"{', '.join(self.labels)}")
            members = [o for o in self._objects() if o.label == label]
            others = [v for v in (value if isinstance(value, list) else [value])
                      if isinstance(v, str) and re.fullmatch(r"[ECH]\d+", v)]
            for ol in others:
                if ol not in known:
                    raise ValueError(f"there's no {ol} on this part — labels are "
                                     f"{', '.join(self.labels)}")
            if action in ("dist", "even", "centre") or (action == "at" and label[0] == "H"):
                continue                 # requirements: met by solve()
            if action == "slot":
                if label[0] != "H":
                    raise ValueError(f"{label} isn't a hole — 'slot' works on holes (H#)")
                sl = _check_slot(value)
                for o in members:
                    o.slot, o.action, o.fastener = self._mirror_slot(sl, o.ops), "slot", None
                continue
            if action == "fastener":
                if label[0] != "H":
                    raise ValueError(f"{label} isn't a hole — screws go in holes (H#)")
                if value and any(o.action == "slot" for o in members):
                    raise ValueError(f"{label} is a slot — screw recesses go on round "
                                     f"holes (add an M hole, or make {label} round first)")
                for o in members:
                    o.fastener = dict(value) if value else None
                    if value:
                        o.action, o.dia = "circle", fx.clearance(value["m"])
                continue
            if action == "samesize":
                # every listed hole takes the first one's diameter
                first = self._primary(label)
                d = first.dia or 2 * _fit_circle(first.dense)[1]
                for ol in others:
                    for o in self._objects():
                        if o.label == ol and isinstance(o, Ring):
                            o.action, o.dia = "circle", d
                if first.action == "auto":
                    for o in members:
                        o.action, o.dia = "circle", d
                continue
            for o in members:
                self._edit_one(o, label, action, value)
            if action in ("move", "offset") and label[0] == "E":
                self.resized = True

    def _edit_one(self, o, label, action, value):
        if action in ("length", "at") and isinstance(o, Segment) and not o.corner:
            return                       # absolute edge targets: solved in solve()
        if action in ("length", "at"):
            raise ValueError(f"{label}: absolute targets work on edges (E#: "
                             f"'{label} is 20mm', '{label} at 30') and holes "
                             f"(H#: 'H1 at 20, 30') — for corners use '{label} R5'")
        if action == "move":
            if isinstance(o, Joint) or (isinstance(o, Segment) and o.corner):
                raise ValueError(f"{label} is a corner — move the edges next to it "
                                 f"instead (corners follow their edges)")
            o.move = o.move + self._mirror_vec(value, o.ops)
            return
        if action == "offset":
            if not isinstance(o, Segment) or o.corner:
                raise ValueError(f"'in'/'out' only works on edges (E#), not {label}")
            o.offset += value
            return
        if isinstance(o, Ring):                          # hole
            if action == "circle" and value:
                o.fastener = None        # an explicit size makes it a plain hole
            if action != "remove":
                o.slot = None            # circle / raw / auto: no longer a slot
            o.action, o.dia = action, value
        elif isinstance(o, Joint):
            if action == "radius":
                if not value:
                    raise ValueError(f"{label}: give a radius, e.g. '{label} R5'")
                o.action, o.radius = "radius", value
            elif action in ("sharp", "auto"):
                o.action, o.radius = "auto", None
            else:
                raise ValueError(f"{label} is a sharp corner — use '{label} R<mm>' "
                                 f"to round it")
        else:
            if o.corner and action == "radius":
                if not value:
                    raise ValueError(f"{label}: give a radius, e.g. '{label} R5'")
                o.action, o.radius = "radius", value
            elif not o.corner and action in ("sharp", "radius"):
                raise ValueError(f"{label} is an edge — corners are the C labels")
            else:
                o.action = action

    # ---- added holes and screws
    def _add_hole(self, v: dict) -> str:
        """'add hole' / 'add M3 …': a new round hole, labelled after the last
        H#, at v['at'] (or the roomiest spot when None). Mirrored like a
        traced hole when the part is symmetric. Returns its label."""
        spec, slot = None, None
        if v.get("slot"):
            slot = _check_slot(v["slot"])
            d = slot["w"]
        elif v.get("m"):
            spec = {"m": v["m"], "style": v.get("style") or "screw",
                    "face": v.get("face") or "top"}
            d = fx.clearance(v["m"])
        else:
            d = float(v["d"])
            if d < 2 * MIN_EDGE:
                raise ValueError(f"a {d:g} mm hole is too small to print — "
                                 f"{2 * MIN_EDGE:g} mm or more")
        c = v.get("at")
        if c is None or any(x is None for x in c):
            body = Polygon(self.outer.dense, [h.dense for h in self.holes]).buffer(0)
            if body.geom_type == "MultiPolygon":
                body = max(body.geoms, key=lambda g: g.area)
            p = polylabel(body, 0.1)
            c = [p.x if c is None or c[0] is None else c[0],
                 p.y if c is None or c[1] is None else c[1]]
        c = np.array(c, float)
        nums = [int(h.label[1:]) for h in self.holes if h.label]
        label = f"H{max(nums, default=0) + 1}"
        # A hole that would overlap its own mirror image goes ON that mirror
        # line instead of being doubled (2026-10-28: an M4 1.2 mm off the
        # centreline of a 'both'-symmetric disc became 4 merged holes).
        reach = d / 2
        if slot:
            reach = slot["l"] / 2
        elif spec:
            reach = max([reach] + [fx.reach(ct) for ct in fx.cuts(**_fx_args(spec))])
        for i, (ax, line) in enumerate((("x", self.length / 2), ("y", self.width / 2))):
            if ax in self.mirrors and 0 < abs(c[i] - line) < reach + MIN_EDGE / 2:
                self.warnings.append(
                    f"{label} put on the {'vertical' if ax == 'x' else 'horizontal'} "
                    f"centreline ({ax.upper()} = {line:.1f}): {abs(c[i] - line):.1f} mm off "
                    f"it, it would have overlapped its own mirror image")
                c[i] = line
        members = [(c, ())]
        for op in self._ops():
            m = self._apply_ops(c, op)
            if all(np.hypot(*(m - cc)) > self.tol for cc, _ in members):
                members.append((m, op))
        for i, (cc, op) in enumerate(members):
            sl = self._mirror_slot(slot, op) if slot else None
            pts = _stadium(cc, sl) if sl else _circle(cc, d / 2)
            ring = Ring([Segment("curve", pts)], pts, hole=True, label=label,
                        action="slot" if sl else "circle", dia=None if sl else d,
                        primary=i == 0, ops=op, slot=sl,
                        fastener=dict(spec) if spec else None, added=True)
            self.holes.append(ring)
        if len(members) > 1:
            self.warnings.append(f"{label} was mirrored to match the part's symmetry")
        return label

    def _mirror_slot(self, slot, op):
        """A slot's angle seen in a mirror (x mirror: 180-a, y mirror: -a)."""
        a = slot["angle"]
        for ax in op:
            a = 180 - a if ax == "x" else -a
        return {**slot, "angle": a % 180}

    def check_holes(self, ext):
        """Added holes and screw holes (their recesses included) must stay
        MIN_EDGE inside the outline and clear of other holes. Raises
        ValueError saying which and how far it misses."""
        body = Polygon(ext).buffer(0)
        inner = body.buffer(-MIN_EDGE)
        shapes = []
        for h in self.holes:
            if not h.label or h.out is None or len(h.out) < 3:
                continue
            c = np.asarray(h.geom.get("c", h.centre), float)
            r = 0.0
            if h.fastener and h.geom.get("type") == "circle":
                r = max(fx.reach(ct) for ct in fx.cuts(**_fx_args(h.fastener)))
            shapes.append((h, Polygon(h.out).buffer(0).union(Point(c).buffer(r, 64))))
        for h, sh in shapes:
            if not (h.added or h.fastener):
                continue
            if not inner.contains(sh):
                what = (f"{h.label}'s {h.fastener['m']} recess" if h.fastener
                        else h.label)
                c = Point(np.asarray(h.geom.get("c", h.centre), float))
                need = c.hausdorff_distance(sh) + MIN_EDGE
                if not body.contains(c):
                    raise ValueError(f"{h.label}'s centre "
                                     f"({c.x:.1f}, {c.y:.1f}) is outside the part — "
                                     f"check X/Y on the drawing (origin = bottom-left)")
                gap = body.exterior.distance(c)
                raise ValueError(
                    f"{what} would reach the edge: it needs {need:.1f} mm from "
                    f"its centre to the edge and has {gap:.1f} mm — move it "
                    f"further in, or pick a smaller size")
            for h2, sh2 in shapes:
                if h2 is not h and sh.distance(sh2) < MIN_EDGE:
                    if h2.label == h.label:
                        if np.hypot(*(np.asarray(h.geom.get("c", h.centre)) -
                                      np.asarray(h2.geom.get("c", h2.centre)))) < self.tol:
                            continue             # the same spot (on a mirror line)
                        raise ValueError(f"{h.label} and its mirror image would run into "
                                         f"each other — move it onto the centreline or "
                                         f"further from it")
                    raise ValueError(f"{h.label} and {h2.label} (with their screw "
                                     f"recesses) would run into each other — move "
                                     f"one of them")

    def fastener_cuts(self) -> list[dict]:
        """Screw recesses / nut pockets in the current frame:
        [{"label", "m", "x", "y", "cuts": [(shape, size, depth, face)]}].
        Mirror partners sit at the mirror image of their primary, exactly."""
        out = []
        for h in self.holes:
            if not (h.label and h.primary and h.fastener):
                continue
            if h.geom.get("type") != "circle":
                continue
            c = np.asarray(h.geom["c"], float)
            cs = fx.cuts(**_fx_args(h.fastener))
            spots = [c] + [self._apply_ops(c, o.ops) for o in self.holes
                           if o.label == h.label and not o.primary]
            for p in spots:
                out.append({"label": h.label, "m": h.fastener["m"],
                            "x": float(p[0]), "y": float(p[1]), "cuts": cs})
        return out

    # ---- assembly
    def _outward(self, ring, d):
        """Outward normal for travel direction d on this ring."""
        ccw = _signed_area(ring.dense) > 0
        n = np.array([d[1], -d[0]]) if ccw else np.array([-d[1], d[0]])
        return -n if ring.hole else n

    def _ring_points(self, ring: Ring, straighten: bool) -> np.ndarray | None:
        if ring.hole:
            act = ring.action
            dense = ring.dense + ring.move
            c, r, rms = _fit_circle(dense)
            if act == "remove":
                ring.out, ring.geom = None, {"type": "removed", "c": c}
                return None
            if act == "slot" and ring.slot:
                c = dense.mean(axis=0)           # a stadium's centroid is its centre
                ring.out = _stadium(c, ring.slot)
                ring.geom = {"type": "slot", "c": c, "w": ring.slot["w"],
                             "l": ring.slot["l"], "angle": ring.slot["angle"]}
                return ring.out
            round_ = act == "circle" or (act == "auto" and straighten
                                         and rms / r < CIRCLE_RMS)
            ell = None if round_ or act != "auto" or not straighten else _fit_ellipse(dense)
            if ell is not None:
                (cx, cy), (ax1, ax2), ang = ell
                if max(ax1, ax2) / min(ax1, ax2) < 1.06:      # basically round
                    round_, r = True, (ax1 + ax2) / 4
                    c = np.array([cx, cy])
                else:
                    n = max(48, int(np.pi * (ax1 + ax2) / 2 / 0.4))
                    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
                    e = np.column_stack([ax1 / 2 * np.cos(a), ax2 / 2 * np.sin(a)])
                    th = np.radians(ang)
                    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
                    ring.out = e @ R.T + [cx, cy]
                    ring.ellipse = (ax1, ax2)
                    ring.geom = {"type": "oval", "c": np.array([cx, cy]),
                                 "a": max(ax1, ax2), "b": min(ax1, ax2)}
                    return ring.out
            if round_:
                if ring.dia:
                    r = ring.dia / 2
                n = max(48, int(2 * np.pi * r / 0.4))
                a = np.linspace(0, 2 * np.pi, n, endpoint=False)
                ring.out = c + r * np.column_stack([np.cos(a), np.sin(a)])
                ring.circle = (c, r)
                ring.geom = {"type": "circle", "c": c, "r": r}
                return ring.out
            ring.out = _simplify(np.vstack([dense, dense[:1]]), 0.3 * self.tol)[:-1]
            ring.geom = {"type": "free", "c": dense.mean(axis=0),
                         "w": float(np.ptp(dense[:, 0])), "h": float(np.ptp(dense[:, 1]))}
            return ring.out

        segs, n = ring.segs, len(ring.segs)
        fine = 0.3 * self.tol
        pts = [s.pts + s.move for s in segs]
        if n == 1:                                      # one closed curve
            s = segs[0]
            if s.action == "arc" or (straighten and s.action == "auto"):
                c, r, rms = _fit_circle(pts[0])
                if s.action == "arc" or rms < CIRCLE_RMS * r:
                    a = np.linspace(0, 2 * np.pi, max(48, int(2 * np.pi * r / 0.4)),
                                    endpoint=False)
                    s.out = c + r * np.column_stack([np.cos(a), np.sin(a)])
                    s.geom = {"type": "arc", "c": c, "r": r}
                    ring.out = s.out
                    return ring.out
            s.out = _simplify(np.vstack([pts[0], pts[0][:1]]), fine)[:-1]
            s.geom = {"type": "free"}
            ring.out = s.out
            return ring.out

        def mode(s, p):
            if s.corner and s.action in ("sharp", "radius"):
                return "drop"                   # replaced by a joint
            if s.corner and (s.action == "arc" or (s.action == "auto" and straighten)):
                c, r, _ = _fit_circle(p)        # rebuilt tangent to its edges
                s.auto_fit = (c, r)
                return "drop"
            if s.action in ("straight", "h", "v"):
                return "line"
            if s.action == "arc":
                return "arc"
            if s.action == "raw" or not straighten:
                return "raw"
            if s.kind == "line":
                return "line"
            c, r, rms = _fit_circle(p)
            return "arc" if rms < CIRCLE_RMS * r and r < 2 * max(self.length, self.width) \
                else "smooth"

        modes = [mode(s, p) for s, p in zip(segs, pts)]
        lines = {}
        for i, (s, md) in enumerate(zip(segs, modes)):
            if md != "line":
                continue
            o, d = _orient(_fit_line(pts[i]), pts[i])
            ang = np.degrees(np.arctan2(d[1], d[0])) % 180
            snap_h = s.action == "h" or (s.action in ("auto", "straight") and straighten
                                         and min(ang, 180 - ang) < SNAP_DEG)
            snap_v = s.action == "v" or (s.action in ("auto", "straight") and straighten
                                         and abs(ang - 90) < SNAP_DEG)
            if snap_h:
                d = np.array([np.sign(d[0]) or 1.0, 0.0])
                o = np.array([o[0], pts[i][:, 1].mean()])
            elif snap_v:
                d = np.array([0.0, np.sign(d[1]) or 1.0])
                o = np.array([pts[i][:, 0].mean(), o[1]])
            d = d / np.hypot(*d)
            o = o + s.offset * self._outward(ring, d)
            lines[i] = (o, d)
        # raw/arc edges with an in/out offset just shift along the chord normal
        for i, s in enumerate(segs):
            if s.offset and i not in lines:
                ch = pts[i][-1] - pts[i][0]
                d = ch / max(np.hypot(*ch), 1e-9)
                pts[i] = pts[i] + s.offset * self._outward(ring, d)

        eff = [i for i in range(n) if modes[i] != "drop"]
        m = len(eff)
        joint_pts = []                          # joint k = start of eff[k]
        for k in range(m):
            a, b = eff[k - 1], eff[k]
            dropped = [segs[j % n] for j in range(a + 1, a + 1 + ((b - a - 1) % n))]
            spec = dropped[0] if dropped else ring.joints[b]
            p = dropped[0].mid if dropped else pts[b][0]
            la, lb = lines.get(a), lines.get(b)
            if la is not None and lb is not None:
                x = _intersect(la, lb)
                forced = (spec is not None and spec.action != "auto") or \
                    segs[a].action != "auto" or segs[b].action != "auto" or \
                    segs[a].move.any() or segs[b].move.any() or \
                    segs[a].offset or segs[b].offset
                # moved/edited edges always meet their neighbours (however far)
                lim = np.inf if forced else \
                    max(3 * self.tol, 0.2 * min(segs[a].length, segs[b].length))
                want_r = spec.radius if spec is not None and spec.action == "radius" else \
                    (spec.auto_fit[1] if getattr(spec, "auto_fit", None) else None)
                if x is None and want_r and la[1] @ lb[1] < -0.99:
                    # parallel sides joined by a full round (e.g. a tab end)
                    c0 = spec.auto_fit[0] if getattr(spec, "auto_fit", None) else p
                    T1, T2 = _project(c0, la), _project(c0, lb)
                    C, r = (T1 + T2) / 2, np.hypot(*(T2 - T1)) / 2
                    a0 = np.arctan2(*(T1 - C)[::-1])
                    a1 = np.arctan2(*(T2 - C)[::-1])
                    bulge = np.arctan2(*la[1][::-1])        # keeps going the way la went
                    span = (a1 - a0) % (2 * np.pi)
                    if (bulge - a0) % (2 * np.pi) > span:
                        span -= 2 * np.pi
                    kk = max(12, int(abs(span) * r / 0.3))
                    ang = a0 + span * np.linspace(0, 1, kk + 1)
                    fp = list(C + r * np.column_stack([np.cos(ang), np.sin(ang)]))
                    joint_pts.append(fp)
                    spec.out, spec.geom = fp, {"type": "radius", "c": C, "r": r}
                    continue
                if x is not None and want_r:
                    f = _fillet(la, lb, x, want_r,
                                0.45 * min(segs[a].length, segs[b].length))
                    if f is not None:
                        fp, C, r_used = f
                        if spec.action == "radius" and r_used < spec.radius - 0.05:
                            self.warnings.append(f"{spec.label}: R{spec.radius:g} doesn't "
                                                 f"fit, used R{r_used:.1f}")
                        joint_pts.append(fp)
                        spec.out, spec.geom = fp, {"type": "radius", "c": C, "r": r_used}
                        continue
                if x is not None and np.hypot(*(x - p)) < lim:
                    joint_pts.append([x])
                    if spec is not None:
                        u, v = -la[1], lb[1]
                        spec.out = [x]
                        spec.geom = {"type": "sharp", "at": x,
                                     "angle": float(np.degrees(np.arccos(np.clip(u @ v, -1, 1))))}
                    continue
                joint_pts.append([_project(p, la), _project(p, lb)])
            elif la is not None:
                joint_pts.append([_project(p, la)])
            elif lb is not None:
                joint_pts.append([_project(p, lb)])
            else:
                joint_pts.append([p])
            if spec is not None:
                spec.out = joint_pts[-1]
                spec.geom = {"type": "sharp", "at": joint_pts[-1][-1], "angle": 0.0}

        out = []
        for k in range(m):
            i = eff[k]
            s = segs[i]
            start = joint_pts[k]
            end = joint_pts[(k + 1) % m][0]
            body = []
            if modes[i] == "raw":
                body = list(_simplify(pts[i], fine)[1:-1])
                s.geom = {"type": "free"}
            elif modes[i] == "smooth":
                body = list(_simplify(_smooth_open(pts[i]), fine)[1:-1])
                s.geom = {"type": "free"}
            elif modes[i] == "arc":
                c, r, _ = _fit_circle(pts[i])
                a0 = np.arctan2(*(start[-1] - c)[::-1])
                a1 = np.arctan2(*(end - c)[::-1])
                mid = pts[i][len(pts[i]) // 2]
                am = np.arctan2(*(mid - c)[::-1])
                span = (a1 - a0) % (2 * np.pi)
                if (am - a0) % (2 * np.pi) > span:      # mid isn't on the ccw way
                    span -= 2 * np.pi
                kk = max(4, int(abs(span) * r / 0.4))
                ang = a0 + span * np.linspace(0, 1, kk + 1)[1:-1]
                body = list(c + r * np.column_stack([np.cos(ang), np.sin(ang)]))
                s.geom = {"type": "arc", "c": c, "r": r}
            else:
                s.geom = {"type": "line"}
            out += list(start) + body
            s.out = np.array([start[-1]] + body + [end])
        for j in range(n):                      # dropped corner segs: drawn via joint
            if modes[j] == "drop":
                segs[j].out = None
        ring.out = np.array(out)
        return ring.out

    def assemble(self, straighten: bool):
        outer = self._ring_points(self.outer, straighten)
        holes = [p for p in (self._ring_points(h, straighten) for h in self.holes)
                 if p is not None]
        poly = Polygon(outer).buffer(0)
        for h in holes:
            poly = poly.difference(Polygon(h).buffer(0))
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
            self.warnings.append("an edit split the outline; kept the largest piece")
        for ax in self.mirrors:                # make symmetry exact
            if ax == "x":
                c = self.length / 2
                half = poly.intersection(box(c, -1e4, 1e4, 1e4))
                poly = unary_union([half, affinity.scale(half, -1, 1, origin=(c, 0))])
            else:
                c = self.width / 2
                half = poly.intersection(box(-1e4, c, 1e4, 1e4))
                poly = unary_union([half, affinity.scale(half, 1, -1, origin=(0, c))])
            poly = poly.buffer(0)
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda g: g.area)
        poly = poly.simplify(0.01)
        ext = np.array(poly.exterior.coords)[:-1]
        ints = [np.array(r.coords)[:-1] for r in poly.interiors]
        return ext, ints

    def warp(self, f, scale):
        """Move the traced inputs (before assembly) into another frame,
        keeping the segmentation and labels; clears assembly results."""
        for ring in [self.outer, *self.holes]:
            ring.dense = f(ring.dense)
            ring.out, ring.circle, ring.ellipse, ring.geom = None, None, None, {}
            for sg in ring.segs:
                sg.pts = f(sg.pts)
                sg.out, sg.geom, sg.auto_fit = None, {}, None
            for j in ring.joints or []:
                if j is not None:
                    j.at = f(np.atleast_2d(j.at))[0]
                    j.out, j.geom = None, {}
        self.tol *= scale

    def remap(self, f, scale, size=None):
        """Move every recorded geometry into the final model frame
        (f: points -> points, scale: for radii). Values within 0.2 mm of the
        part's boundary snap onto it (pre-symmetry geometry can overhang)."""
        f0 = f
        if size is not None:
            L, W = size

            def f(p):
                q = f0(p)
                for i, lim in ((0, L), (1, W)):
                    q[:, i] = np.where(np.abs(q[:, i]) < 0.2, 0.0, q[:, i])
                    q[:, i] = np.where(np.abs(q[:, i] - lim) < 0.2, lim, q[:, i])
                return q

        def g(geom):
            for k in ("c", "at"):
                if k in geom:
                    geom[k] = f(np.atleast_2d(geom[k]))[0]
            for k in ("r", "a", "b", "w", "h", "l"):
                if k in geom:
                    geom[k] = geom[k] * scale
        for o in self._objects():
            if getattr(o, "out", None) is not None and len(o.out):
                o.out = f(np.atleast_2d(np.array(o.out, float)))
            g(o.geom)

    # ---- absolute targets
    def _primary(self, label):
        return next(o for o in self._objects() if o.label == label and o.primary)

    def _hole_c(self, label):
        h = self._primary(label)
        if not isinstance(h, Ring):
            raise ValueError(f"{label} isn't a hole")
        if h.geom.get("type") == "removed":
            raise ValueError(f"{label} was removed — it has no position")
        c = h.geom.get("c")
        return np.asarray(c if c is not None else h.centre, float)

    def _line_of(self, label):
        seg = self._primary(label)
        if not isinstance(seg, Segment) or seg.corner:
            raise ValueError(f"{label} isn't an edge — distances go between edges (E#) "
                             f"and holes (H#)")
        if seg.geom.get("type") != "line" or seg.out is None:
            raise ValueError(f"{label} is a curve — make it straight first "
                             f"('{label} straight')")
        a, b = seg.out[0], seg.out[-1]
        return a, (b - a) / max(np.hypot(*(b - a)), 1e-9)

    def _measure(self, req, ext):
        """Current value of a requirement on the assembled shape."""
        k = req["kind"]
        if k == "size":
            ax = 0 if req["axis"] == "x" else 1
            return float(np.ptp(ext[:, ax]))
        if k == "hole":
            ax = 0 if req["axis"] == "x" else 1
            return float(self._hole_c(req["label"])[ax] - ext[:, ax].min())
        if k == "centre":
            ax = 0 if req["axis"] == "x" else 1
            return float(self._hole_c(req["label"])[ax] - (ext[:, ax].min() + ext[:, ax].max()) / 2)
        if k == "even":
            # equal centre-to-centre distances (doesn't force them into a line)
            p = [self._hole_c(lab) for lab in req["labels"]]
            i = req["i"]
            return float(np.hypot(*(p[i + 1] - p[i])) - np.hypot(*(p[i] - p[i - 1])))
        if k == "dist":
            a, b = req["label"], req["other"]
            if a[0] == "E" and b[0] == "H":
                a, b = b, a
            if a[0] == "H" and b[0] == "H":
                return float(np.hypot(*(self._hole_c(a) - self._hole_c(b))))
            if a[0] == "H":
                o, d = self._line_of(b)
                v = self._hole_c(a) - o
                return float(abs(v[0] * d[1] - v[1] * d[0]))
            o1, d1 = self._line_of(a)
            o2, d2 = self._line_of(b)
            if abs(d1[0] * d2[1] - d1[1] * d2[0]) > np.sin(np.radians(2)):
                raise ValueError(f"{a} and {b} aren't parallel — a distance between "
                                 f"edges needs parallel edges")
            sa = self._primary(a).out
            m = (sa[0] + sa[-1]) / 2 - o2
            return float(abs(m[0] * d2[1] - m[1] * d2[0]))
        seg = self._primary(req["label"])
        if seg.geom.get("type") != "line" or seg.out is None:
            raise ValueError(f"{req['label']} is a curve — make it straight first "
                             f"('{req['label']} straight'), then give its "
                             f"{'length' if req['kind'] == 'length' else 'position'}")
        a, b = seg.out[0], seg.out[-1]
        if req["kind"] == "length":
            return float(np.hypot(*(b - a)))
        # positions are in the drawing's frame: from the part's bottom-left,
        # which moves if an outside edge moves
        dx, dy = b - a
        if abs(dy) < 1e-3:
            return float(a[1] - ext[:, 1].min())
        if abs(dx) < 1e-3:
            return float(a[0] - ext[:, 0].min())
        raise ValueError(f"{req['label']} is sloped — 'at' works on level or upright "
                         f"edges; give its length instead ('{req['label']} is 20mm')")

    def _check_valid(self, ext, ints, base_dirs):
        """None if the part still makes sense, else why not."""
        for seg in self.outer.segs:
            if not seg.label or seg.geom.get("type") != "line" or seg.out is None:
                continue
            d = seg.out[-1] - seg.out[0]
            ln = np.hypot(*d)
            d0 = base_dirs.get(id(seg))
            if ln < MIN_EDGE or (d0 is not None and d @ d0 <= 0):
                return f"{seg.label} would shrink to nothing or flip over"
        ring = self.outer.out
        if ring is not None and len(ring) >= 3 and not Polygon(ring).is_valid:
            return "the outline would cross over itself (an area would collapse)"
        # each hole's own outline (a hole that breaks the edge stops being a
        # hole after the cut, so check before that)
        body = Polygon(ring if ring is not None else ext).buffer(0).buffer(-MIN_EDGE)
        hs = [(h.label, Polygon(h.out).buffer(0)) for h in self.holes
              if h.out is not None and len(h.out) >= 3]
        for lab, hp in hs:
            if not body.contains(hp):
                return f"{lab} would break through (or come within {MIN_EDGE} mm of) the edge"
        for i, (la, a) in enumerate(hs):
            for lb, b in hs[i + 1:]:
                if a.distance(b) < MIN_EDGE:
                    return f"{la} and {lb} would run into each other"
        return None

    def solve(self, reqs, straighten):
        """Slide straight edges (each along its own normal; mirror partners
        together) by the least total amount that meets every requirement.
        Returns (ok, ext, ints, info): info has 'residual' {req id: miss mm},
        'why' (validity failure) and 'moves' {label: outward mm}."""
        keep_warn = list(self.warnings)
        ext, ints = self.assemble(straighten)
        edge_labs = []
        for o in self.outer.segs:
            if o.label and o.primary and not o.corner and o.label not in edge_labs \
                    and o.geom.get("type") == "line":
                edge_labs.append(o.label)
        # holes only become variables when a requirement mentions them
        hole_refs = set()
        for r in reqs:
            for lab in [r.get("label"), r.get("other"), *r.get("labels", [])]:
                if lab and lab[0] == "H":
                    hole_refs.add(lab)
        hole_labs = [h.label for h in self.holes if h.primary and h.label in hole_refs]
        # variable k -> (label, axis or None); cost: moving an edge reshapes the
        # part, moving a hole doesn't — edges cost 20x, so holes move first
        labels = [(lab, None) for lab in edge_labs] + \
            [(lab, ax) for lab in hole_labs for ax in (0, 1)]
        weight = np.array([20.0 if ax is None else 1.0 for _, ax in labels])
        members = {lab: [o for o in self._objects() if o.label == lab]
                   for lab in edge_labs + hole_labs}
        base = {lab: [(o.offset if isinstance(o, Segment) else o.move.copy())
                      for o in members[lab]] for lab in edge_labs + hole_labs}
        base_dirs = {id(o): o.out[-1] - o.out[0] for o in self.outer.segs
                     if o.out is not None and len(o.out) > 1}
        targets = np.array([r["value"] for r in reqs], float)
        for r in reqs:                                   # raises if unmeasurable
            self._measure(r, ext)

        def setx(x):
            hv = {}
            for (lab, ax), xi in zip(labels, x):
                if ax is None:
                    for o, b in zip(members[lab], base[lab]):
                        o.offset = b + xi
                else:
                    hv.setdefault(lab, np.zeros(2))[ax] = xi
            for lab, v in hv.items():
                for o, b in zip(members[lab], base[lab]):
                    o.move = b + self._mirror_vec(v, o.ops)

        def evaluate(x):
            setx(x)
            e, i = self.assemble(straighten)
            return np.array([self._measure(r, e) for r in reqs]) - targets, e, i

        def newton(active):
            x = np.zeros(len(labels))
            F, _, _ = evaluate(x)
            h = 0.5
            for _ in range(12):
                if not len(F) or np.max(np.abs(F)) < 0.005 or not active:
                    break
                J = np.zeros((len(reqs), len(active)))
                for c, k in enumerate(active):
                    x2 = x.copy()
                    x2[k] += h
                    F2, _, _ = evaluate(x2)
                    J[:, c] = (F2 - F) / h
                w = weight[active]
                d = np.linalg.lstsq(J / w, -F, rcond=None)[0] / w   # least weighted movement
                if not np.all(np.isfinite(d)):
                    break
                step = np.max(np.abs(d)) if len(d) else 0
                if step > 50:
                    d *= 50 / step
                for _ in range(6):                          # backtrack if worse
                    xn = x.copy()
                    xn[active] += d
                    Fn, _, _ = evaluate(xn)
                    if np.all(np.isfinite(Fn)) and np.linalg.norm(Fn) < np.linalg.norm(F):
                        break
                    d = d / 2
                else:
                    break
                x, F = xn, Fn
            return x

        # Edges can't pass through each other: if the least-movement answer
        # breaks the part, freeze the edge that moved most and re-solve, so
        # the change goes to edges that have room.
        active = list(range(len(labels)))
        x = newton(active)
        broke = None                     # first way a near-solution broke the part
        for _ in range(len(labels)):
            F, ext, ints = evaluate(x)
            bad = self._check_valid(ext, ints, base_dirs)
            if (not len(F) or np.max(np.abs(F)) < 0.02) and bad is None:
                break
            broke = broke or bad
            moved = [k for k in active if abs(x[k]) > 0.05]
            if not moved:
                break
            # inward edge moves are what collapse material: freeze those first
            inward = [k for k in moved if x[k] < 0 and labels[k][1] is None]
            active.remove(min(inward, key=lambda k: x[k]) if inward
                          else max(moved, key=lambda k: abs(x[k])))
            x = newton(active)
        self.warnings = keep_warn
        F, ext, ints = evaluate(x)                       # leave state consistent
        why = self._check_valid(ext, ints, base_dirs)
        ok = (not len(F) or np.max(np.abs(F)) < 0.02) and why is None
        moves = {}
        for (lab, ax), xi in zip(labels, x):
            if ax is None:
                if abs(xi) >= 0.05:
                    moves[lab] = float(xi)
            else:
                v = moves.get(lab, np.zeros(2))
                v = np.array(v, float)
                v[ax] = xi
                moves[lab] = v
        moves = {k: v for k, v in moves.items()
                 if np.ndim(v) == 0 or np.hypot(*v) >= 0.05}
        info = {"why": why or (broke if not ok else None), "moves": moves,
                "values": [float(f + t) for f, t in zip(F, targets)]}
        return ok, ext, ints, info

    # ---- reporting
    def legend(self) -> list[dict]:
        """One row per label (primaries): what it is + exact geometry, in
        the final model frame (origin = part's bottom-left, mm)."""
        rows = []
        L, W = self.length, self.width
        for lab in self.labels:
            o = next(o for o in self._objects() if o.label == lab and o.primary)
            act = getattr(o, "action", "auto")
            note = "" if act == "auto" else ACTION_NAMES.get(act, act)
            if isinstance(o, Ring) and (o.fastener or o.added):
                note = "added" if o.added else ""
            if isinstance(o, Ring):
                gm = o.geom
                c = gm.get("c", o.centre)
                if gm.get("type") == "removed":
                    desc, val = "Hole (removed)", "—"
                elif gm.get("type") == "slot":
                    desc = "Slot"
                    val = (f"{gm['w']:.1f} wide × {gm['l']:.1f} long, {gm['angle']:.0f}°;  "
                           f"center ({c[0]:.1f}, {c[1]:.1f})")
                elif gm.get("type") == "circle" and o.fastener:
                    desc, detail = fx.describe(**_fx_args(o.fastener))
                    val = f"{detail};  center ({c[0]:.1f}, {c[1]:.1f})"
                elif gm.get("type") == "circle":
                    desc, val = "Round hole", f"Ø{2 * gm['r']:.1f} thru;  center ({c[0]:.1f}, {c[1]:.1f})"
                elif gm.get("type") == "oval":
                    desc, val = "Oval hole", (f"{gm['a']:.1f} × {gm['b']:.1f} thru;  "
                                              f"center ({c[0]:.1f}, {c[1]:.1f})")
                else:
                    desc, val = "Hole (as traced)", (f"{gm.get('w', 0):.1f} × {gm.get('h', 0):.1f};  "
                                                     f"center ({c[0]:.1f}, {c[1]:.1f})")
                kind = "hole"
            elif isinstance(o, Joint) or o.corner:
                gm = o.geom
                kind = "corner"
                if gm.get("type") == "radius" or (gm.get("type") == "arc"):
                    c = gm["c"]
                    desc = "Rounded corner"
                    val = f"R{gm['r']:.1f}, center ({c[0]:.1f}, {c[1]:.1f})"
                elif gm.get("type") == "sharp":
                    a = gm["at"]
                    ang = gm.get("angle", 0)
                    desc = "Sharp corner"
                    val = (f"{ang:.0f}° at ({a[0]:.1f}, {a[1]:.1f})" if ang
                           else f"at ({a[0]:.1f}, {a[1]:.1f})")
                elif gm.get("type") == "line":
                    p = o.out
                    desc, val = "Chamfered corner", (f"({p[0][0]:.1f}, {p[0][1]:.1f}) → "
                                                     f"({p[-1][0]:.1f}, {p[-1][1]:.1f})")
                else:
                    p = o.out if o.out is not None else o.pts
                    m = p[len(p) // 2]
                    desc, val = "Rounded corner (as traced)", f"near ({m[0]:.1f}, {m[1]:.1f})"
            else:
                kind = "edge"
                p = o.out if o.out is not None else o.pts
                a, b = p[0], p[-1]
                ln = _arc_len(p)
                if o.geom.get("type") == "line":
                    dx, dy = b - a
                    if abs(dy) < 1e-3:
                        y = a[1]
                        desc = ("Top edge" if y > W - 0.5 else "Bottom edge" if y < 0.5
                                else "Level edge")
                        val = f"Y = {y:.1f};  X {min(a[0], b[0]):.1f} → {max(a[0], b[0]):.1f}  ({ln:.1f} long)"
                    elif abs(dx) < 1e-3:
                        x = a[0]
                        desc = ("Right side" if x > L - 0.5 else "Left side" if x < 0.5
                                else "Upright edge")
                        val = f"X = {x:.1f};  Y {min(a[1], b[1]):.1f} → {max(a[1], b[1]):.1f}  ({ln:.1f} long)"
                    else:
                        ang = np.degrees(np.arctan2(dy, dx)) % 180
                        desc = "Sloped edge"
                        val = (f"({a[0]:.1f}, {a[1]:.1f}) → ({b[0]:.1f}, {b[1]:.1f});  "
                               f"{ln:.1f} long, {ang:.0f}°")
                elif o.geom.get("type") == "arc":
                    c = o.geom["c"]
                    desc = "Curved edge (arc)"
                    val = f"R{o.geom['r']:.1f}, center ({c[0]:.1f}, {c[1]:.1f});  {ln:.1f} long"
                else:
                    desc = "Edge (as traced)" if o.kind == "line" else "Curved edge (as traced)"
                    val = (f"({a[0]:.1f}, {a[1]:.1f}) → ({b[0]:.1f}, {b[1]:.1f});  "
                           f"{ln:.1f} long")
            mv = getattr(o, "move", np.zeros(2))
            if np.any(mv) or getattr(o, "offset", 0):
                note = (note + " " if note else "") + "moved"
            rows.append({"label": lab, "kind": kind, "action": act,
                         "desc": desc + (f" [{note}]" if note else ""), "value": val})
        return rows


def _circle(c, r):
    a = np.linspace(0, 2 * np.pi, max(48, int(2 * np.pi * r / 0.4)), endpoint=False)
    return np.asarray(c, float) + r * np.column_stack([np.cos(a), np.sin(a)])


def _stadium(c, slot):
    """Slot outline: two half circles of Ø w joined by straight sides; l is
    the overall end-to-end length, angle (deg) its direction from +X."""
    w, l, ang = slot["w"], slot["l"], np.radians(slot["angle"])
    r, half = w / 2, (l - w) / 2
    n = max(16, int(np.pi * r / 0.4))
    t1 = np.linspace(-np.pi / 2, np.pi / 2, n)
    t2 = np.linspace(np.pi / 2, 3 * np.pi / 2, n)
    p = np.vstack([np.column_stack([half + r * np.cos(t1), r * np.sin(t1)]),
                   np.column_stack([-half + r * np.cos(t2), r * np.sin(t2)])])
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    return p @ R.T + np.asarray(c, float)


def _check_slot(slot: dict) -> dict:
    w, l = float(slot["w"]), float(slot["l"])
    if w < 2 * MIN_EDGE:
        raise ValueError(f"a {w:g} mm wide slot is too narrow to print — "
                         f"{2 * MIN_EDGE:g} mm or more")
    if l <= w:
        raise ValueError(f"a slot's length ({l:g}) must be more than its width "
                         f"({w:g}) — for a round hole use 'add hole {w:g}'")
    return {"w": w, "l": l, "angle": float(slot.get("angle") or 0) % 180}


def _fx_args(spec: dict) -> dict:
    return {"size": spec["m"], "style": spec["style"], "face": spec["face"]}


def edit_text(e) -> str:
    """One stored edit as the text a user would type (for version notes)."""
    lab, act, val = (list(e) + [None, None])[:3]
    if lab == "ADD":
        at = val.get("at")
        where = f" at {at[0]:g}, {at[1]:g}" if at and None not in at else ""
        if val.get("slot"):
            sl = val["slot"]
            return f"add slot {sl['w']:g} {sl['l']:g} angle {sl.get('angle', 0):g}{where}"
        if val.get("m"):
            return f"add {val['m']} {val.get('style', 'screw')} {val.get('face', 'top')}{where}"
        return f"add hole {val['d']:g}{where}"
    if act == "fastener":
        return f"{lab} {val['m']} {val['style']} {val['face']}" if val else f"{lab} plain"
    if act == "slot":
        return f"{lab} slot {val['w']:g} {val['l']:g} angle {val.get('angle', 0):g}"
    if lab == "SCALE":
        return {"length": f"scale to {val:g} long", "width": f"scale to {val:g} wide",
                "factor": f"scale {val * 100:g}%"}[act]
    return " ".join(str(x) for x in e if x is not None)


def _signed_area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * (x @ np.roll(y, -1) - y @ np.roll(x, -1))


# ------------------------------------------------------------------- edits

class ConflictError(ValueError):
    """A new absolute target can't be met together with earlier ones.

    message     human explanation (show to the user)
    new         descriptions of the requirement(s) just asked for
    conflicts   [{id, desc}] earlier requirements that block it — dropping
                any one of them (override) makes the new one possible
    can_override False when nothing earlier is to blame (the shape itself
                can't do it)
    """
    def __init__(self, message, new, conflicts, can_override=True, together=False):
        super().__init__(message)
        self.new, self.conflicts, self.can_override = new, conflicts, can_override
        self.together = together      # True: all conflicts must go, else any one


def req_desc(r) -> str:
    k = r["kind"]
    if "desc" in r:
        return r["desc"]
    if k == "size":
        return (f"the overall {'length' if r['axis'] == 'x' else 'width'} "
                f"of {r.get('show', r['value']):g} mm")
    if k == "length":
        return f"{r['label']} = {r['value']:g} mm long"
    if k == "dist":
        return f"{r['label']} to {r['other']} = {r['value']:g} mm"
    return f"{r['label']} at {r['value']:g}"


def req_name(r) -> str:
    """Short unique handle for 'override <name>'."""
    k = r["kind"]
    if k == "size":
        return "length" if r["axis"] == "x" else "width"
    if k == "dist":
        return f"{r['label']}-{r['other']}"
    if k == "even":
        return ",".join(r["labels"]) + " spacing"
    if k == "centre":
        return f"{r['label']} centreline"
    if k == "hole":
        return f"{r['label']} position"
    if k == "at":
        return f"{r['label']} position"
    return r["label"]


def _fmt_xy(v):
    return ", ".join("–" if c is None else f"{c:g}" for c in v)


def requirements(edits, size, free_size=(), shown=None):
    """Hard requirements: absolute edge targets from the edit list (latest
    wins per label+kind) + the overall size unless freed by an override."""
    groups = {}                 # latest edit wins per (label, action, partner)
    for i, (lab, act, val) in enumerate(edits):
        rid = f"edit:{i}"
        g = None
        if lab[0] == "E" and act in ("length", "at"):
            g = [{"label": lab, "kind": act, "value": float(val)}]
            key = (lab, act)
        elif lab[0] == "H" and act == "at":
            d = f"{lab} at ({_fmt_xy(val)})"
            g = [{"label": lab, "kind": "hole", "axis": ax, "value": float(v), "desc": d}
                 for ax, v in zip("xy", val) if v is not None]
            key = (lab, "at")
        elif act == "dist":
            other, d = val
            g = [{"label": lab, "kind": "dist", "other": other, "value": float(d)}]
            key = ("dist",) + tuple(sorted((lab, other)))
        elif act == "even":
            labs = [lab] + list(val)
            d = f"{', '.join(labs)} evenly spaced"
            g = [{"labels": labs, "label": lab, "kind": "even", "i": j,
                  "value": 0.0, "desc": d}
                 for j in range(1, len(labs) - 1)]
            key = ("even",) + tuple(labs)
        elif act == "centre":
            d = (f"{lab} on the {'vertical' if val == 'x' else 'horizontal'} centreline")
            g = [{"label": lab, "kind": "centre", "axis": val, "value": 0.0, "desc": d}]
            key = (lab, "centre")
        if g:
            for r in g:
                r.update(id=rid, edit_index=i)
            groups[key] = g
    out = [r for g in groups.values() for r in g]
    for ax, v in zip("xy", size):
        if ax not in free_size:
            out.append({"id": f"size:{ax}", "label": "", "kind": "size", "axis": ax,
                        "value": float(v),
                        "show": round(float((shown or size)["xy".index(ax)]), 2)})
    return out


def solve_or_explain(shape, reqs, new_ids, straighten, version=None):
    """Solve; on failure raise ConflictError naming the earlier
    requirement(s) whose removal would make the new one possible.
    Requirements sharing an id (one edit -> several equations) go together."""
    ok, ext, ints, info = shape.solve(reqs, straighten)
    if ok:
        return ext, ints, info
    ids = list(dict.fromkeys(r["id"] for r in reqs))
    group = {i: [r for r in reqs if r["id"] == i] for i in ids}
    new = [i for i in ids if i in new_ids]
    prior = [i for i in ids if i not in new_ids]
    why = info["why"]

    def without(*drop):
        return [r for r in reqs if r["id"] not in drop]

    def feasible(rs):
        return shape.solve(rs, straighten)[0]

    # what single-valued new targets come out as when everything earlier is kept
    closest = {}
    p_rs = without(*new)
    p_ok, p_ext, _, _ = shape.solve(p_rs, straighten)
    for i in new:
        g = group[i]
        if len(g) == 1 and g[0]["kind"] in ("length", "at", "dist") and p_ok:
            try:
                closest[i] = shape._measure(g[0], p_ext)
            except ValueError:
                pass

    blockers = [i for i in prior if feasible(without(i))]
    together = False                     # True: both must go, not either one
    if not blockers and len(prior) <= 6:
        for k, a in enumerate(prior):
            for b in prior[k + 1:]:
                if feasible(without(a, b)):
                    blockers, together = [a, b], True
                    break
            if blockers:
                break

    def desc(i):
        return req_desc(group[i][0])

    def name(i):
        return req_name(group[i][0])

    asked = " and ".join(desc(i) for i in new)
    near = "; ".join(
        f"with that kept, {desc(i).split(' = ')[0].split(' at ')[0]} "
        f"{'is' if group[i][0]['kind'] != 'at' else 'sits at'} {v:.1f}"
        f"{'' if group[i][0]['kind'] == 'at' else ' mm'}"
        for i, v in closest.items() if abs(v - group[i][0]["value"]) > 0.02)
    keep = f"keep v{version}" if version else "keep the current version"
    if not blockers:
        msg = (f"Can't make {asked}: "
               f"{why or 'no edge or hole that is free to move can reach it'}"
               f"{' — ' + near.replace('with that kept, ', 'as it stands, ') if near else ''}"
               f". This isn't caused by an earlier requirement, so it can't be "
               f"overridden; try a different value.")
        raise ConflictError(msg, [desc(i) for i in new], [], can_override=False)
    # most recent edit first, the overall size last
    blockers.sort(key=lambda i: (group[i][0]["kind"] == "size",
                                 -group[i][0].get("edit_index", 0)))
    alts = (" and " if together else " or ").join(desc(i) for i in blockers)
    names = (" + " if together else " or ").join(f"'override {name(i)}'" for i in blockers)
    if len(blockers) > 1 and not together:
        names += " (either one is enough)"
    msg = (f"Conflict: {asked} isn't possible while keeping {alts}"
           f"{' (' + why + ')' if why else ''}"
           f"{' — ' + near if near else ''}. Reply 'cancel' to {keep}, or {names} "
           f"to drop {'those earlier requirements' if together else 'that earlier requirement'}"
           f" and apply the new one.")
    raise ConflictError(msg, [desc(i) for i in new],
                        [{"id": i, "label": name(i), "desc": desc(i)} for i in blockers],
                        together=together)


def parse_edits(text: str) -> list[tuple[str, str, object]]:
    """'E3 up 11, C2 R5; H1 circle 6, E1 E5 level' -> [(label, action, value)]

    Edges E#: straight, arc, level, plumb, raw, auto; up/down/left/right N,
              out/in N (mm, along the edge's outward normal)
    Corners C#: sharp, R N (radius), straight (chamfer), arc, raw, auto
    Holes H#: circle [Ø N], remove, raw, auto; up/down/left/right N
    Edge finish (whole part): 'all edges R1', 'top edges chamfer 0.5',
      'bottom edges sharp'
    Relations (requirements): 'H1 to E3 is 10', 'H1 H2 apart 40',
      'E1 to E3 is 30', 'H1 H2 H3 evenly spaced', 'H1 on centreline',
      'H1 H2 same size'
    Absolute targets (hard requirements, kept across versions):
      'E3 is 20mm' / 'E3 20' / 'E3 length 20'   edge length
      'E3 at 30' / 'E3 y 30' / 'E3 x=40'        level/upright edge position
      'H1 at 20, 30' / 'H1 x 20 y 30'           hole centre
    """
    edits = []
    lab_re = re.compile(r"[EeCcHh]\d+")
    num_re = re.compile(r"[+-]?\d+(\.\d+)?(mm)?")
    # "H1 at 20, 30": a comma between two numbers after 'at' is a coordinate
    text = re.sub(r"(\bat\s*\(?\s*[+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d)", r"\1 \2",
                  text or "", flags=re.I)
    for clause in re.split(r"[,;\n]+", text):
        raw = clause.strip()
        words = re.sub(r"[()=]", " ", raw).replace("Ø", " ").replace("ø", " ") \
            .replace("′", "").split()
        special = (_parse_scale(words) or _parse_add(words) or _parse_fastener(words)
                   or _parse_finish(words) or _parse_multi(words) or _parse_absolute(words))
        if special:
            edits += special
            continue
        if not words:
            continue
        labels = [w.upper() for w in words if lab_re.fullmatch(w)]
        rest = [w.lower() for w in words if not lab_re.fullmatch(w)]
        # "R5" / "r5" = radius 5
        for w in list(rest):
            m = re.fullmatch(r"r(\d+(\.\d+)?)(mm)?", w)
            if m:
                rest[rest.index(w)] = "radius"
                rest.append(m.group(1))
        nums = [float(re.sub(r"mm$", "", w)) for w in rest if num_re.fullmatch(w)]
        acts = [w for w in rest if not num_re.fullmatch(w) and w not in FILLER]
        if not labels or not acts:
            raise ValueError(f"couldn't read {raw!r} — use e.g. 'E3 straight', "
                             f"'E3 up 2', 'C2 R5', 'C4 sharp', 'H1 circle 6', 'H2 remove'")
        for lab in labels:
            table = {"E": EDGE_ACTIONS, "C": CORNER_ACTIONS, "H": HOLE_ACTIONS}[lab[0]]
            if lab[0] == "C" and any(w in MOVES for w in acts):
                raise ValueError(f"{lab} is a corner — move the edges next to it "
                                 f"instead (corners follow their edges)")
            word = next((w for w in acts if w in table or (w in MOVES and lab[0] in "EH")),
                        None)
            if word is None:
                opts = sorted({ACTION_NAMES.get(v, v) for v in table.values()})
                if lab[0] in "EH":
                    opts += ["up/down/left/right N"] + (["out/in N"] if lab[0] == "E" else [])
                raise ValueError(f"{lab}: don't know {acts[0]!r} — options are "
                                 f"{', '.join(opts)}")
            val = nums[0] if nums else None
            if word in MOVES and word not in table:
                if val is None:
                    raise ValueError(f"{lab} {word}: by how much? e.g. '{lab} {word} 2'")
                mv = MOVES[word]
                if mv in ("out", "in"):
                    if lab[0] != "E":
                        raise ValueError(f"'{word}' only works on edges (E#)")
                    edits.append((lab, "offset", val if mv == "out" else -val))
                else:
                    edits.append((lab, "move", [mv[0] * val, mv[1] * val]))
            else:
                edits.append((lab, table[word], val))
    return edits


def _fx_words(rest):
    """M-size, style and face words in a clause (None where not given)."""
    size = next((fx.norm_size(w) for w in rest if fx.norm_size(w)), None)
    style = next((fx.STYLE_WORDS[w] for w in rest if w in fx.STYLE_WORDS), None)
    face = next((w for w in rest if w in fx.FACES), None)
    if face is None and any(w in ("underneath", "under", "bed") for w in rest):
        face = "bottom"
    return size, style, face


def _xy(rest):
    """'at 20 10' / 'x 20 y 10' -> [x, y] (None parts allowed), or None."""
    num = re.compile(r"[+-]?\d+(\.\d+)?(mm)?$")
    xy = {}
    for i, w in enumerate(rest[:-1]):
        if w in ("x", "y") and num.match(rest[i + 1]):
            xy[w] = float(rest[i + 1].replace("mm", ""))
    if xy:
        return [xy.get("x"), xy.get("y")]
    if "at" in rest:
        after = [float(w.replace("mm", "")) for w in rest[rest.index("at") + 1:]
                 if num.match(w)]
        if len(after) >= 2:
            return after[:2]
        raise ValueError("give the centre as 'at X, Y', e.g. 'add M3 at 20, 10'")
    return None


def _parse_add(words):
    """'add hole 5 at 20, 10' / 'add M3 [screw|nut|screw+nut] [top|bottom]
    at 20, 10' (no position = the roomiest spot on the part)."""
    w = [x.lower() for x in words]
    if not w or w[0] != "add":
        return None
    rest = w[1:]
    at = _xy(rest)
    if "slot" in rest:
        return [("ADD", "hole", {"slot": _slot_nums(rest), "at": at})]
    size, style, face = _fx_words(rest)
    if size:
        style = style or "screw"
        face = face or ("bottom" if style == "nut" else "top")
        return [("ADD", "hole", {"m": size, "style": style, "face": face, "at": at})]
    if any(re.fullmatch(r"m\d+(\.\d+)?", x) for x in rest):
        raise ValueError(f"screw sizes are {', '.join(fx.SIZES)}")
    stop = rest.index("at") if "at" in rest else len(rest)
    nums = [float(x.replace("mm", "")) for x in rest[:stop]
            if re.fullmatch(r"\d+(\.\d+)?(mm)?", x) and x not in ("x", "y")]
    if "x" in rest[:stop]:
        nums = [float(x.replace("mm", "")) for x in rest[:rest.index("x")]
                if re.fullmatch(r"\d+(\.\d+)?(mm)?", x)]
    if not nums:
        raise ValueError("add what? e.g. 'add hole 5 at 20, 10' (Ø5 mm) or "
                         "'add M3 at 20, 10' (screw hole)")
    return [("ADD", "hole", {"d": nums[0], "at": at})]


def _slot_nums(rest):
    """'slot 6 20 [angle 45]' (width, overall length) -> slot dict."""
    stop = rest.index("at") if "at" in rest else len(rest)
    for k in ("x", "y"):                       # 'x 20 y 10' position words
        if k in rest[:stop] and rest.index(k) + 1 < len(rest) and \
                re.fullmatch(r"[+-]?\d+(\.\d+)?(mm)?", rest[rest.index(k) + 1]):
            stop = min(stop, rest.index(k))
    num = re.compile(r"[+-]?\d+(\.\d+)?(mm|°|deg)?$")
    angle = 0.0
    words = rest[:stop]
    for k in ("angle", "at-angle", "rotated", "deg"):
        if k in words:
            i = words.index(k)
            after = [x for x in words[i + 1:] if num.match(x)]
            if after:
                angle = float(re.sub(r"(mm|°|deg)$", "", after[0]))
                words = words[:i] + [x for x in words[i + 1:] if x != after[0]]
    if "vertical" in words or "upright" in words:
        angle = 90.0
    nums = [float(re.sub(r"(mm|°|deg)$", "", x)) for x in words if num.match(x)]
    if len(nums) < 2:
        raise ValueError("a slot needs width and length, e.g. 'add slot 6 20 at 30, 10' "
                         "(6 wide, 20 long; add 'angle 90' to turn it)")
    return {"w": nums[0], "l": nums[1], "angle": angle}


def _parse_scale(words):
    """'scale to 120 long' / 'scale length 120' / 'scale width 40' /
    'scale 150%' / 'scale 1.5x' -> [("SCALE", length|width|factor, value)]."""
    w = [x.lower() for x in words]
    if not w or w[0] not in ("scale", "resize"):
        return None
    txt = " ".join(w[1:])
    m = re.search(r"(\d+(\.\d+)?)\s*%", txt)
    if m:
        return [("SCALE", "factor", float(m.group(1)) / 100)]
    m = re.search(r"(\d+(\.\d+)?)\s*x\b", txt)
    if m and not re.search(r"\b(long|length|wide|width)\b", txt):
        return [("SCALE", "factor", float(m.group(1)))]
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", txt)]
    if not nums:
        raise ValueError("scale to what? e.g. 'scale to 120 long', 'scale width 40' "
                         "or 'scale 150%'")
    axis = "width" if re.search(r"\b(wide|width)\b", txt) else "length"
    return [("SCALE", axis, nums[0])]


def _parse_fastener(words):
    """'H1 M3' / 'H2 M4 nut bottom' / 'H1 M3 screw+nut' / 'H1 plain'."""
    labels = [x.upper() for x in words if re.fullmatch(r"[Hh]\d+", x)]
    rest = [x.lower() for x in words if not re.fullmatch(r"[EeCcHh]\d+", x)]
    if not labels or len(labels) != len([x for x in words if re.fullmatch(r"[EeCcHh]\d+", x)]):
        return None
    if "slot" in rest:
        return [(lab, "slot", _slot_nums(rest)) for lab in labels]
    if set(rest) & {"plain", "noscrew"} or rest[-2:] == ["no", "screw"]:
        return [(lab, "fastener", None) for lab in labels]
    size, style, face = _fx_words(rest)
    if not size:
        if any(re.fullmatch(r"m\d+(\.\d+)?", x) for x in rest):
            raise ValueError(f"screw sizes are {', '.join(fx.SIZES)}")
        return None
    style = style or "screw"
    face = face or ("bottom" if style == "nut" else "top")
    return [(lab, "fastener", {"m": size, "style": style, "face": face}) for lab in labels]


def _parse_absolute(words):
    """Absolute-target clauses, or None if this isn't one."""
    lab_re = re.compile(r"[EeHh]\d+")
    num_re = re.compile(r"[+-]?\d+(\.\d+)?(mm)?$")
    labels = [w.upper() for w in words if lab_re.fullmatch(w)]
    if not labels or any(re.fullmatch(r"[Cc]\d+", w) for w in words):
        return None
    rest = [w.lower() for w in words if not lab_re.fullmatch(w)]
    nums = [float(w.replace("mm", "")) for w in rest if num_re.match(w)]
    wordset = {w for w in rest if not num_re.match(w)}
    if not nums or wordset & (set(MOVES) | set(EDGE_ACTIONS) | set(HOLE_ACTIONS)
                              | {"r", "radius"}):
        return None
    out = []
    xy = {}
    for i, w in enumerate(rest[:-1]):
        if w in ("x", "y") and num_re.match(rest[i + 1]):
            xy[w] = float(rest[i + 1].replace("mm", ""))
    for lab in labels:
        if lab[0] == "H":
            if "at" not in wordset and not xy:
                return None
            if xy:
                out.append((lab, "at", [xy.get("x"), xy.get("y")]))
            elif len(nums) >= 2:
                out.append((lab, "at", [nums[0], nums[1]]))
            else:
                raise ValueError(f"{lab}: give the centre as 'at X, Y' (e.g. "
                                 f"'{lab} at 20, 30') or 'x 20' / 'y 30'")
        else:
            if "at" in wordset or xy:
                out.append((lab, "at", list(xy.values())[0] if xy else nums[0]))
            elif wordset <= {"is", "long", "length", "len", "be", "should", "make",
                             "to", "mm", "exactly", "needs", "need"}:
                out.append((lab, "length", nums[0]))
            else:
                return None
    return out


def _parse_finish(words):
    """'all edges R1' / 'top edges chamfer 0.5' / 'bottom edges sharp'."""
    w = [x.lower() for x in words]
    if not any(x in ("edge", "edges") for x in w):
        return None
    if any(re.fullmatch(r"[ech]\d+", x) for x in w):
        return None
    sides = ["TOP", "BOTTOM"] if ("all" in w or not ({"top", "bottom"} & set(w))) else \
        [x.upper() for x in ("top", "bottom") if x in w]
    num = None
    kind = None
    for i, x in enumerate(w):
        m = re.fullmatch(r"r(\d+(\.\d+)?)(mm)?", x)
        if m:
            kind, num = "fillet", float(m.group(1))
        elif x in ("r", "radius", "round", "rounded", "fillet", "filleted"):
            kind = "fillet"
        elif x in ("chamfer", "chamfered", "bevel"):
            kind = "chamfer"
        elif x in ("sharp", "square", "none", "plain"):
            kind, num = "none", 0.0
        elif re.fullmatch(r"\d+(\.\d+)?(mm)?", x) and num is None:
            num = float(x.replace("mm", ""))
    if kind is None:
        return None
    if kind != "none" and not num:
        raise ValueError("give a size, e.g. 'top edges R1' or 'all edges chamfer 0.5'")
    return [(side, kind, num) for side in sides]


def _parse_multi(words):
    """Relations between features: distances, spacing, centreline, same size."""
    lab_re = re.compile(r"[EeHh]\d+")
    labels = [x.upper() for x in words if lab_re.fullmatch(x)]
    rest = [x.lower() for x in words if not lab_re.fullmatch(x)]
    nums = [float(x.replace("mm", "")) for x in rest if re.fullmatch(r"\d+(\.\d+)?(mm)?", x)]
    wset = {x for x in rest if not re.fullmatch(r"\d+(\.\d+)?(mm)?", x)}
    if not labels:
        return None
    if wset & {"evenly", "equally", "even", "equal"} and (wset & {"spaced", "spacing", "even",
                                                                  "evenly", "equally"}):
        if len(labels) < 3 or any(l[0] != "H" for l in labels):
            raise ValueError("evenly spaced needs 3 or more holes, e.g. 'H1 H2 H3 evenly spaced'")
        return [(labels[0], "even", labels[1:])]
    if wset & {"same", "match", "matching"} and wset & {"size", "sized", "diameter", "same"}:
        if len(labels) < 2 or any(l[0] != "H" for l in labels):
            raise ValueError("same size works on holes, e.g. 'H1 H2 same size'")
        return [(labels[0], "samesize", labels[1:])]
    cl = {"centreline", "centerline", "centred", "centered", "centre", "center", "middle"}
    if wset & cl and not nums:
        axis = "y" if wset & {"horizontal", "y"} else "x"
        if any(l[0] != "H" for l in labels):
            raise ValueError("'on centreline' works on holes (H#)")
        return [(l, "centre", axis) for l in labels]
    if len(labels) == 2 and nums and (wset <= {"to", "is", "apart", "distance", "from", "be",
                                               "and", "between", "mm", "should", "needs",
                                               "need", "exactly", "away", "centre", "center"}):
        return [(labels[0], "dist", [labels[1], nums[0]])]
    return None
