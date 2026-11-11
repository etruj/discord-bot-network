#!/usr/bin/env python3
"""boxmeta.py — the per-box `box.json` metadata file.

A generated box is more than a pile of STLs: `/inserts` needs to know a box's
*interior* so it can build something that fits, and a future library
(`/boxes`, `/boxinfo`) needs to know what each folder is. That knowledge can't
be recovered reliably from an STL — a bounding box is exterior and says nothing
about wall thickness or rounded corners — so every build records it here.

`box.json` is deliberately the single source of truth: `/inserts` lists only
boxes that carry one. Boxes built before this file existed simply won't appear
until they're rebuilt, which is safer than guessing their interior from meshes.

Schema (only the keys we actually use today):

    {"name": "foo", "type": "rugged", "version": 1,
     "internal": {"width": 180, "length": 90, "height": 40},
     "insert": {"supported": true, "corner_radius": 5.0},
     "printer": "prusa_core_one"}

`internal.height` is the usable cavity depth an insert can occupy.
`insert.corner_radius` lets a tray match the box's rounded interior so it drops
in instead of binding on the corners.
"""
import json
from pathlib import Path

FILENAME = "box.json"


def make(name: str, box_type: str, internal: dict, *,
         insert_supported: bool, corner_radius: float = 0.0,
         printer: str = "prusa_core_one", version: int = 1) -> dict:
    """Assemble a metadata dict in the canonical shape."""
    return {
        "name": name,
        "type": box_type,
        "version": version,
        "internal": {"width": internal["width"],
                     "length": internal["length"],
                     "height": internal["height"]},
        "insert": {"supported": bool(insert_supported),
                   "corner_radius": float(corner_radius)},
        "printer": printer,
    }


def write(box_dir: Path, meta: dict) -> Path:
    """Write `box.json` into a box folder. Returns the path."""
    box_dir = Path(box_dir)
    box_dir.mkdir(parents=True, exist_ok=True)
    path = box_dir / FILENAME
    path.write_text(json.dumps(meta, indent=2))
    return path


def read(box_dir: Path):
    """Return a box folder's metadata, or None if it has none / is unreadable."""
    path = Path(box_dir) / FILENAME
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def scan(boxes_root, insert_only: bool = True) -> list:
    """Every box.json under the boxes root, newest folder first.

    With `insert_only`, keep only boxes whose metadata marks them insert-ready —
    exactly the set `/inserts` should offer. Folders without a box.json are
    skipped, so this never surfaces a box we can't measure.
    """
    root = Path(boxes_root)
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir(),
                        key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        meta = read(child)
        if meta is None:
            continue
        if insert_only and not meta.get("insert", {}).get("supported"):
            continue
        out.append(meta)
    return out
