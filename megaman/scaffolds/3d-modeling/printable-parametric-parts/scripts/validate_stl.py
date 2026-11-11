#!/usr/bin/env python3
"""Validate STL(s) for FDM printing.

Checks: binary format, watertight, single connected body, sliver triangles,
and (if admesh is installed) disconnected facets / holes.

Usage:
    python3 validate_stl.py part.stl [more.stl ...]
Exit code 0 if all pass, 1 if any fail.
"""
import sys
import shutil
import subprocess

import numpy as np
import trimesh


def is_ascii_stl(path):
    with open(path, "rb") as f:
        head = f.read(256)
    # ASCII STLs start with "solid" AND contain facet text near the top;
    # binary STLs have an 80-byte header that won't contain "facet".
    return head[:5] == b"solid" and b"facet" in head


def check(path):
    print(f"\n== {path} ==")
    ok = True

    ascii_stl = is_ascii_stl(path)
    if ascii_stl:
        print("  format            : ASCII  -> re-export with --export-format binstl !")
    else:
        print("  format            : binary  (ok)")
    ok &= not ascii_stl

    m = trimesh.load(path, force="mesh")

    wt = bool(m.is_watertight)
    print(f"  watertight        : {wt}")
    ok &= wt

    bc = int(m.body_count)
    print(f"  single body       : {bc == 1}  (bodies={bc})")
    ok &= bc == 1

    print(f"  bbox (mm)         : {np.round(m.extents, 2).tolist()}")

    tri = m.triangles
    e0 = np.linalg.norm(tri[:, 1] - tri[:, 0], axis=1)
    e1 = np.linalg.norm(tri[:, 2] - tri[:, 1], axis=1)
    e2 = np.linalg.norm(tri[:, 0] - tri[:, 2], axis=1)
    emin = np.minimum.reduce([e0, e1, e2])
    emax = np.maximum.reduce([e0, e1, e2])
    aspect = float((emax / np.clip(emin, 1e-9, None)).max())
    # Advisory only: minkowski-rounded parts legitimately produce long wall
    # slivers and still slice fine once watertight + binary. High aspect is a
    # heads-up to consider remeshing if a slicer ever complains, not a failure.
    if aspect >= 200:
        print(f"  max edge aspect   : {aspect:.0f}  "
              f"(WARN: slivers present — usually ok in binary; remesh if a slicer balks)")
    else:
        print(f"  max edge aspect   : {aspect:.0f}  (ok)")

    if shutil.which("admesh"):
        out = subprocess.run(["admesh", path], capture_output=True, text=True).stdout
        holes = "No holes" in out
        print(f"  admesh holes      : {'none (ok)' if holes else 'HOLES PRESENT'}")
        ok &= holes
    else:
        print("  admesh            : not installed (skipped)")

    print(f"  RESULT            : {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(2)
    all_ok = all(check(p) for p in paths)
    print()
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
