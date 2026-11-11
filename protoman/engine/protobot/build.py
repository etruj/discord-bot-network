"""Run OpenSCAD (binary STL + preview PNG) and validate the mesh."""
from __future__ import annotations

import os
import shutil
import subprocess

import numpy as np
import trimesh

OPENSCAD = os.environ.get("PROTOBOT_OPENSCAD", "openscad")
TIMEOUT_S = 300


def _cmd(args: list[str]) -> list[str]:
    cmd = [OPENSCAD, *args]
    if not os.environ.get("DISPLAY") and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a", *cmd]
    return cmd


def _run(args: list[str]) -> str:
    p = subprocess.run(_cmd(args), capture_output=True, text=True, timeout=TIMEOUT_S)
    log = p.stdout + p.stderr
    if p.returncode != 0 or "ERROR" in log:
        raise RuntimeError(f"openscad failed ({p.returncode}):\n{log[-2000:]}")
    return log


def export_stl(scad: str, stl: str) -> str:
    """Binary STL only (ASCII can trip slicers). Manifold backend: fast and
    robust for big traced polygons."""
    return _run(["--backend=manifold", "--export-format", "binstl", "-o", stl, scad])


def render_preview(scad: str, png: str, size=(900, 700)) -> None:
    _run(["--backend=manifold", "--render", "--viewall", "--autocenter",
          "--colorscheme", "Tomorrow", "--camera", "0,0,0,55,0,25,0",
          "--imgsize", f"{size[0]},{size[1]}", "-o", png, scad])


def validate(stl: str) -> dict:
    """Print-readiness gate: binary, watertight, one body,
    admesh finds no holes."""
    with open(stl, "rb") as f:
        head = f.read(256)
    binary = not (head[:5] == b"solid" and b"facet" in head)
    m = trimesh.load(stl, force="mesh")
    res = {
        "binary": binary,
        "watertight": bool(m.is_watertight),
        "bodies": int(m.body_count),
        "extents_mm": np.round(m.extents, 2).tolist(),
        "volume_mm3": round(float(m.volume), 1),
        "triangles": int(len(m.faces)),
    }
    if shutil.which("admesh"):
        out = subprocess.run(["admesh", stl], capture_output=True, text=True).stdout
        res["admesh_no_holes"] = "No holes" in out
    res["ok"] = (res["binary"] and res["watertight"] and res["bodies"] == 1
                 and res.get("admesh_no_holes", True))
    return res
