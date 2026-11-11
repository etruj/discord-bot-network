"""Metric screw and nut sizes for screw holes (ISO M2-M8).

A screw hole is a clearance hole through the part plus, on one face, a
round recess the head sits in flush, and/or a hex pocket for a nut. The head
recess is sized to fit EITHER a pan head (ISO 7045, the taller one) or a
countersunk head (ISO 10642/7046, the wider one): a countersunk head in a flat
recess rests on the hole's rim and ends up at or below the surface.

Numbers are nominal max sizes from the standards plus FDM clearance:
    hole   ISO 273 "medium" clearance
    head   +0.3 mm per side, +0.2 mm deep, rounded up to whole layers
    nut    +0.1 mm per side across flats (slides in by hand: dogbag's
           tested M4 = 7.2), +0.2 mm deep, whole layers
"""
from __future__ import annotations

import math

LAYER = 0.2
MIN_FLOOR = 1.2           # mm of plastic left under a recess (6 layers)

#        clearance, head Ø (max pan/csk), head height (pan), nut AF, nut height
_ISO = {
    "M2":   (2.4,  4.0,   1.6, 4.0,  1.6),
    "M2.5": (2.9,  5.0,   2.1, 5.0,  2.0),
    "M3":   (3.4,  6.72,  2.4, 5.5,  2.4),
    "M4":   (4.5,  8.96,  3.1, 7.0,  3.2),
    "M5":   (5.5,  11.2,  3.7, 8.0,  4.7),
    "M6":   (6.6,  13.44, 4.6, 10.0, 5.2),
    "M8":   (9.0,  17.92, 6.0, 13.0, 6.8),
}
SIZES = list(_ISO)
STYLES = ("screw", "nut", "both")
FACES = ("top", "bottom")
STYLE_WORDS = {"screw": "screw", "head": "screw", "bolt": "both", "both": "both",
               "screw+nut": "both", "nut": "nut"}


def _layers(mm: float) -> float:
    return round(math.ceil(mm / LAYER - 1e-6) * LAYER, 2)


def _up(mm: float) -> float:
    """Round up to 0.1 mm (a recess must never come out smaller)."""
    return round(math.ceil(mm * 10 - 1e-6) / 10, 1)


def norm_size(word: str) -> str | None:
    """'m3' / 'M3' / 'm2.5' -> 'M3' / 'M2.5' (None if not a size we stock)."""
    w = word.strip().upper().replace(",", ".")
    return w if w in _ISO else None


def clearance(size: str) -> float:
    return _ISO[size][0]


def cuts(size: str, style: str, face: str) -> list[tuple]:
    """[(shape, size_mm, depth_mm, face)] for one screw hole. shape 'circle'
    (size = Ø) or 'hex' (size = across flats). For 'both', `face` is the
    head's face and the nut goes on the other one."""
    _, hd, hh, af, nh = _ISO[size]
    head = ("circle", _up(hd + 0.6), _layers(hh + 0.2))
    nut = ("hex", _up(af + 0.2), _layers(nh + 0.2))
    other = "bottom" if face == "top" else "top"
    if style == "screw":
        return [(*head, face)]
    if style == "nut":
        return [(*nut, face)]
    return [(*head, face), (*nut, other)]


def reach(c: tuple) -> float:
    """Radius a cut reaches from the hole centre (a hex's corners)."""
    shape, size, _, _ = c
    return size / 2 / math.cos(math.radians(30)) if shape == "hex" else size / 2


def need_thickness(cs: list[tuple]) -> float:
    """Thinnest part these cuts fit in, keeping MIN_FLOOR between them."""
    return round(sum(c[2] for c in cs) + MIN_FLOOR, 2)


def describe(size: str, style: str, face: str) -> tuple[str, str]:
    """(short name, detail) for the feature map, e.g.
    ('M3 screw hole', 'Ø3.4 thru, head Ø7.4 × 2.6 deep (top)')."""
    name = {"screw": f"{size} screw hole", "nut": f"{size} nut hole",
            "both": f"{size} screw + nut hole"}[style]
    parts = [f"Ø{clearance(size):g} thru"]
    for shape, s, d, f in cuts(size, style, face):
        what = f"head Ø{s:g}" if shape == "circle" else f"nut {s:g} AF"
        parts.append(f"{what} × {d:g} deep ({f})")
    return name, ", ".join(parts)
