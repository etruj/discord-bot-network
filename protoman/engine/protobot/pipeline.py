"""One call from an image + measurements to a validated, print-ready part.

The bot's two options, one function each, plus a revise step:

    from protobot import sketch_to_part, photo_to_part, revise_part
    r = sketch_to_part("tracing.jpg", 120, 40, 4, symmetry="lr", straighten=True)
    r = photo_to_part("object_on_paper.jpg", thickness=4)   # size from paper
    r.ok, r.stl, r.preview, r.overlay, r.drawing, r.labels, r.summary()
    r2 = revise_part(r.job_dir, "E3 up 2, C2 R5, H1 circle 6")

Both trace into the same Trace type and share everything after tracing
(refine.py: symmetry / straighten / labels / edits; drawing.py: the
ballooned feature map; then sizing, .scad, export, validation); the tracers themselves are separate modules
(sketch.py, photo.py) so they can be tuned independently.
"""
from __future__ import annotations

import json
import re

import numpy as np
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import build, drawing, fasteners, refine
from .common import Trace, overlay
from .model import LAYER_H, build_model, place_version, to_scad
from .photo import trace_photo
from .sketch import trace_sketch

JOBS_DIR = Path(__file__).resolve().parent.parent / "jobs"


@dataclass
class Result:
    ok: bool
    job_dir: str
    scad: str
    stl: str
    preview: str | None
    overlay: str
    drawing: str                   # ballooned feature map PNG (E#/C#/H# + table)
    drawing_pdf: str
    length: float
    width: float
    thickness: float
    layer_height: float
    trace_mode: str
    rotated: bool
    stretch: float
    points: int
    holes: int
    version: int
    symmetry: str | None
    straighten: bool
    edits: list                    # [[label, action, value], ...] in order
    labels: list[dict]             # table rows: label, kind, action, desc, value
    validation: dict
    warnings: list[str] = field(default_factory=list)
    requirements: list = field(default_factory=list)   # descriptions in force
    locked: list = field(default_factory=list)         # labels tied to a requirement
    finish: dict = field(default_factory=dict)         # {"TOP": [kind, mm], ...}

    def summary(self) -> str:
        """Short human-readable report (what the bot would post)."""
        v = self.validation
        opts = [o for o in (self.symmetry and f"symmetric {self.symmetry}",
                            self.straighten and "straightened",
                            self.edits and f"{len(self.edits)} edit(s)") if o]
        lines = [
            f"{'✅' if self.ok else '❌'} v{self.version}  {self.length:g} x "
            f"{self.width:g} x {self.thickness:g} mm  ({self.trace_mode}"
            f"{', ' + ', '.join(opts) if opts else ''}; "
            f"{self.holes} hole{'s' * (self.holes != 1)})",
            f"mesh: {'watertight' if v['watertight'] else 'NOT watertight'}, "
            f"{v['bodies']} body, {v['volume_mm3'] / 1000:.1f} cm³, "
            f"print at {self.layer_height} mm layers",
            f"drawing: {len(self.labels)} labelled features "
            f"({', '.join(r['label'] for r in self.labels)}) — reply by label to edit",
        ]
        lines += [f"⚠ {w}" for w in self.warnings]
        return "\n".join(lines)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40] or "part"


def _job_dir(src: Path, name: str, out_dir: str | None) -> Path:
    job = Path(out_dir) if out_dir else JOBS_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
    job.mkdir(parents=True, exist_ok=True)
    dst = job / f"input{src.suffix.lower()}"
    if src.resolve() != dst.resolve():
        shutil.copy(src, dst)
    return job


def _scaled(e, k: float):
    """An earlier edit after the part is scaled by k: positions and target
    lengths/distances scale with it (an added hole stays at the same spot
    on the part); feature sizes (Ø, R, slot size, M screws, edge finish)
    and small nudges (moves) stay in mm."""
    lab, act, val = e
    if lab == "ADD" and val.get("at"):
        val = {**val, "at": [None if v is None else round(v * k, 2) for v in val["at"]]}
    elif act == "at" and lab[0] == "H":
        val = [None if v is None else round(v * k, 2) for v in val]
    elif act in ("length", "at") and lab[0] == "E":
        val = round(float(val) * k, 2)
    elif act == "dist":
        val = [val[0], round(float(val[1]) * k, 2)]
    return (lab, act, val)


def _edits(edits) -> list:
    """Edits as an ordered list of (label, action, value)."""
    if not edits:
        return []
    if isinstance(edits, str):
        return refine.parse_edits(edits)
    if isinstance(edits, dict):                      # older job.json format
        return [(k, *v) for k, v in edits.items()]
    return [tuple(e) for e in edits]


def sketch_to_part(image: str, length: float, width: float, thickness: float, *,
                   name: str | None = None, out_dir: str | None = None,
                   mode: str = "auto", invert: bool = False, holes: bool = True,
                   orient: str = "auto", smooth: float = 2.0,
                   symmetry: str | None = None, straighten: bool = False,
                   edits=None, version: int = 1, preview: bool = True,
                   free_size=(), new_from: int = 0, parent=None, note="") -> Result:
    """Option 1 — a drawn tracing/silhouette. The sketch gives the shape,
    length x width x thickness give the size.

    symmetry    None | "lr" (left↔right mirror) | "tb" (top↔bottom) | "both"
    straighten  straight edges -> true lines (snapped level/plumb), round
                holes -> circles
    edits       "E3 up 2, E1 straight, C2 R5, H1 circle 6" (labels from
                drawing.png)

    Raises ValueError for bad input with a message fit to show the user."""
    src = Path(image)
    name = _slug(name or src.stem)
    t = trace_sketch(str(src), mode=mode, invert=invert, smooth=smooth, holes=holes)
    params = dict(kind="sketch", name=name, length=length, width=width,
                  thickness=thickness, mode=mode, invert=invert, holes=holes,
                  orient=orient, smooth=smooth)
    return _finish(t, src, name, _job_dir(src, name, out_dir), length, width,
                   thickness, orient, version, preview, symmetry, straighten,
                   _edits(edits), params, free_size=free_size, new_from=new_from,
                   parent=parent, note=note)


def photo_to_part(image: str, thickness: float, length: float | None = None,
                  width: float | None = None, *, name: str | None = None,
                  out_dir: str | None = None, paper: str = "letter",
                  holes: bool = True, orient: str = "auto", smooth: float = 2.0,
                  symmetry: str | None = None, straighten: bool = False,
                  edits=None, version: int = 1, preview: bool = True,
                  free_size=(), new_from: int = 0, parent=None, note="") -> Result:
    """Option 2 — a photo of the real object lying on paper, from above.
    length/width are optional when the paper's 4 corners are visible: the
    size is then measured off the paper (and reported either way).
    symmetry / straighten / edits as in sketch_to_part."""
    src = Path(image)
    name = _slug(name or src.stem)
    if (length is None) != (width is None):
        raise ValueError("give both length and width, or neither (to measure "
                         "them off the paper)")
    params = dict(kind="photo", name=name, length=length, width=width,
                  thickness=thickness, paper=paper, holes=holes, orient=orient,
                  smooth=smooth)
    t = trace_photo(str(src), paper=paper, holes=holes, smooth=smooth)
    t = refine.prepare(t, symmetry, straighten)     # square up before measuring
    measured = t.size_mm
    if length is None:
        if not measured:
            raise ValueError("couldn't measure the object: the paper's 4 "
                             "corners weren't all visible. Give the length and "
                             "width, or retake the photo with the whole sheet "
                             "in frame")
        length, width = (round(v, 1) for v in measured)
        orient = "as-drawn"
        t.warnings.append(f"size measured off the paper: {length} x {width} mm "
                          f"(±1–2 mm; give exact numbers if it matters)")
    elif measured:
        t.warnings.append(f"for reference, the paper measures it at "
                          f"{measured[0]:.0f} x {measured[1]:.0f} mm")
    return _finish(t, src, name, _job_dir(src, name, out_dir), length, width,
                   thickness, orient, version, preview, symmetry, straighten,
                   _edits(edits), params, prepared=True, free_size=free_size,
                   new_from=new_from, parent=parent, note=note)


def _family(job: Path) -> dict[int, Path]:
    """All versions of a job: {version: folder} (v1 = the base folder)."""
    base = re.sub(r"_v\d+$", "", job.name)
    fam = {}
    for d in [job.parent / base, *job.parent.glob(f"{base}_v*")]:
        m = re.fullmatch(re.escape(base) + r"(?:_v(\d+))?", d.name)
        if m and (d / "job.json").exists():
            fam[int(m.group(1) or 1)] = d
    return fam


def history(job_dir: str) -> list[dict]:
    """Every version of this part, oldest first: version, folder, parent
    version, what was asked for in that step, and whether it's the latest."""
    fam = _family(Path(job_dir))
    out = []
    for v in sorted(fam):
        meta = json.loads((fam[v] / "job.json").read_text())
        out.append({"version": v, "job_dir": str(fam[v]), "parent": meta.get("parent"),
                    "note": meta.get("note") or ("original" if v == 1 else ""),
                    "edits": meta.get("edits", []), "latest": v == max(fam)})
    return out


def revert_part(job_dir: str, to_version: int, *, preview: bool = True) -> Result:
    """Go back to an earlier version: rebuilds it as a NEW latest version
    (nothing is deleted, so you can always go forward again)."""
    fam = _family(Path(job_dir))
    if to_version not in fam:
        raise ValueError(f"there's no v{to_version} — versions are "
                         f"{', '.join(f'v{v}' for v in sorted(fam))}")
    return revise_part(str(fam[to_version]), "", preview=preview,
                       note=f"revert to v{to_version}")


def undo_part(job_dir: str, *, preview: bool = True) -> Result:
    """Undo the latest change: back to the version the latest was made from."""
    fam = _family(Path(job_dir))
    latest = max(fam)
    meta = json.loads((fam[latest] / "job.json").read_text())
    parent = meta.get("parent") or latest - 1
    if latest == 1 or parent not in fam:
        raise ValueError("nothing to undo — this is the original version")
    return revise_part(str(fam[parent]), "", preview=preview,
                       note=f"undo v{latest} (back to v{parent})")


def revise_part(job_dir: str, edits: str = "", *, symmetry="keep",
                straighten=None, out_dir: str | None = None,
                preview: bool = True, override=False, note: str = "") -> Result:
    """Re-run a finished job with label edits (and/or changed symmetry /
    straighten) as the next version. Edits accumulate across revisions;
    labels stay the same because they come from the original trace.

        revise_part("jobs/..._bracket", "E3 up 11, C2 R5, H2 remove")
        revise_part("jobs/..._bracket", "E3 is 20mm")

    Absolute targets ("E3 is 20mm", "E3 at 30", "H1 at 20, 30") stay as
    requirements in later versions. If a new one can't be met together with
    an earlier one (or the overall size), refine.ConflictError is raised and
    nothing is written: the user can cancel, or call again with
    override=True (drop every blocker listed in the error) or
    override=["E5", "width"] (drop just those).
    """
    job = Path(job_dir)
    meta = json.loads((job / "job.json").read_text())
    p = dict(meta["params"])
    old = _edits(meta.get("edits"))
    new = _edits(edits)
    # "scale to 120 long" / "scale 150%": a new overall size with the same
    # proportions. It changes the part's length/width (the size everything
    # is fitted to), not the edit list, so screw holes stay true M sizes and
    # sizes you typed (Ø, R, moves) stay in mm.
    scales = [e for e in new if e[0] == "SCALE"]
    new = [e for e in new if e[0] != "SCALE"]
    if scales:
        cur = json.loads((job / "result.json").read_text())
        _, axis, val = scales[-1]
        k = float(val) if axis == "factor" else float(val) / float(cur[axis])
        if not 0.05 <= k <= 20:
            raise ValueError(f"that's a {k:.2f}x scale — give a size between 5% "
                             f"and 2000% of the current part")
        p["length"] = round(p["length"] * k, 2)
        p["width"] = round(p["width"] * k, 2)
        old = [_scaled(e, k) for e in old]
    free = list(meta.get("free_size", []))
    sym = meta.get("symmetry") if symmetry == "keep" else symmetry
    stra = meta.get("straighten", False) if straighten is None else straighten
    fam = _family(job)
    ver = max(fam) + 1                  # never reuse a number, even from an old version
    src = next(job.glob("input.*"))
    base = re.sub(r"_v\d+$", "", job.name)
    out = Path(out_dir) if out_dir else job.parent / f"{base}_v{ver}"
    parent = int(meta.get("version", 1))
    note = note or (edits if isinstance(edits, str) else "")

    def run(old_edits, free_size):
        existed = out.exists()
        common = dict(name=p["name"], out_dir=str(out), holes=p["holes"],
                      orient=p["orient"], smooth=p["smooth"], symmetry=sym,
                      straighten=stra, edits=old_edits + new, version=ver,
                      preview=preview, free_size=tuple(free_size),
                      new_from=len(old_edits), parent=parent, note=note)
        try:
            if p["kind"] == "sketch":
                return sketch_to_part(str(src), p["length"], p["width"], p["thickness"],
                                      mode=p["mode"], invert=p["invert"], **common)
            return photo_to_part(str(src), p["thickness"], p["length"], p["width"],
                                 paper=p["paper"], **common)
        except Exception:
            if not existed:                  # nothing half-built left behind
                shutil.rmtree(out, ignore_errors=True)
            raise

    try:
        return run(old, free)
    except refine.ConflictError as e:
        if not override or not e.can_override:
            raise
        if override is True:
            # alternatives: drop the first (most recent) one; a pair: drop both
            drop = e.conflicts if e.together else e.conflicts[:1]
            wanted = None
        else:
            wanted = {str(o).lower() for o in override}
            drop = [c for c in e.conflicts
                    if c["label"].lower().replace(" ", "") in
                    {w.replace(" ", "") for w in wanted} or c["id"] in wanted]
        if not drop:
            raise refine.ConflictError(
                f"{', '.join(sorted(wanted))} isn't what blocks this — " + str(e),
                e.new, e.conflicts) from e
        drop_idx = {int(c["id"].split(":")[1]) for c in drop if c["id"].startswith("edit:")}
        kept = [ed for i, ed in enumerate(old) if i not in drop_idx]
        # the edit list numbering includes earlier edits for the same label;
        # drop every earlier absolute target on that label+kind
        gone = {(old[i][0], old[i][1]) for i in drop_idx}
        kept = [ed for ed in kept if (ed[0], ed[1]) not in gone]
        free += [c["id"].split(":")[1] for c in drop if c["id"].startswith("size:")]
        r = run(kept, free)
        r.warnings.append("override: dropped " + ", ".join(c["desc"] for c in drop))
        return r


def _finish(t: Trace, src: Path, name: str, job: Path, length, width, thickness,
            orient, version, preview, symmetry, straighten, edits, params,
            prepared=False, free_size=(), new_from=0, parent=None,
            note="") -> Result:
    """Shared tail: refine -> sized model -> .scad -> STL -> gate."""
    if not prepared:
        t = refine.prepare(t, symmetry, straighten)
    ov = job / "trace.png"
    overlay(t, str(ov))

    held = {}

    def refiner(to_mm, rotated):
        # Pass 1: label + assemble the unedited part; that fixes the frame
        # where it is exactly length x width. Then the SAME labelled segments
        # are moved into that frame (labels can't shift between versions)
        # and the edits applied there, so every number the user typed
        # (moves, R, Ø) comes out exact rather than nudged by the final fit.
        sh = refine.Shape.from_trace(t, to_mm, rotated, length, width)
        ext, ints = sh.assemble(straighten)
        if edits:
            lo0, hi0 = ext.min(axis=0), ext.max(axis=0)
            k0 = np.array([length, width]) / (hi0 - lo0)
            sh.warp(lambda p: (np.asarray(p, float) - lo0) * k0, float(k0.mean()))
            sh.apply_edits(edits)
            ext, ints = sh.assemble(straighten)
            # Absolute targets ("E3 is 20mm", "E3 at 30") are hard
            # requirements, together with the overall size; straight edges
            # slide by the least total amount to meet them all.
            size = np.ptp(ext, axis=0)
            shown = [u if abs(m - u) < 0.1 else m for m, u in zip(size, (length, width))]
            reqs = refine.requirements(edits, size, free_size, shown)
            if any(r["kind"] != "size" for r in reqs):
                new_ids = {r["id"] for r in reqs if r.get("edit_index", -1) >= new_from}
                ext, ints, info = refine.solve_or_explain(sh, reqs, new_ids, straighten,
                                                         version - 1 or None)
                if info["moves"]:
                    sh.warnings.append(
                        "to meet " + ", ".join(dict.fromkeys(
                            refine.req_desc(r) for r in reqs if r["kind"] != "size"))
                        + ": moved " +
                        ", ".join(
                            (f"{lab} {'out' if v > 0 else 'in'} {abs(v):.1f} mm"
                             if np.ndim(v) == 0 else
                             f"{lab} by ({v[0]:+.1f}, {v[1]:+.1f}) mm")
                            for lab, v in info["moves"].items()))
                if free_size:
                    sh.resized = True
        if edits:
            sh.check_holes(ext)
        held.update(shape=sh, ext=ext, ints=ints,
                    reqs=refine.requirements(edits, np.ptp(ext, axis=0), free_size,
                                             (length, width)) if edits else
                    refine.requirements([], (length, width), free_size))
        return ext, ints, not sh.resized

    finish = {}
    for lab, kind, val in edits:                  # latest wins per side
        if lab in ("TOP", "BOTTOM"):
            finish[lab] = (kind, float(val or 0))
    m = build_model(t, length, width, thickness, orient=orient, version=version,
                    refiner=refiner, finish=finish)
    shape = held["shape"]
    if (m.length, m.width) != (length, width):
        m.warnings.append(f"the edits changed the overall size to "
                          f"{m.length:g} x {m.width:g} mm (was {length:g} x {width:g})")
        length, width = m.length, m.width
    # Put the drawing in the exact frame of the .scad / STL.
    ext = held["ext"]
    lo, hi = ext.min(axis=0), ext.max(axis=0)
    k = np.array([length, width]) / (hi - lo)       # ~1 once edits are in final frame

    def to_final(p):
        return (np.asarray(p, float) - lo) * k

    shape.remap(to_final, float(k.mean()), (float(length), float(width)))
    shape.length, shape.width = float(length), float(width)
    # Screw recesses / nut pockets, in the final frame. Each needs the part
    # thick enough to leave MIN_FLOOR of plastic.
    m.fasteners = shape.fastener_cuts()
    for f in m.fasteners:
        need = fasteners.need_thickness(f["cuts"])
        if thickness < need - 1e-6:
            raise ValueError(
                f"{f['label']}: a {f['m']} recess needs the part to be at least "
                f"{need:g} mm thick (it's {thickness:g} mm) — make it thicker, use "
                f"a smaller screw, or a plain hole ('{f['label']} plain')")
    if any(c[3] == "bottom" for f in m.fasteners for c in f["cuts"]):
        place_version(m)                 # keep the stamp out of bottom pockets
    dpng, dpdf = job / "drawing.png", job / "drawing.pdf"
    f_ext, f_ints = to_final(ext), [to_final(h) for h in held["ints"]]
    (job / "outline.json").write_text(json.dumps(
        {"outer": f_ext.round(4).tolist(), "holes": [h.round(4).tolist() for h in f_ints]}))
    # what to show next to the part: requirements in force, finish, and
    # what changed since the version this one was made from
    reqs = held["reqs"]
    for r in reqs:
        if r["kind"] == "size":
            r["show"] = float({"x": length, "y": width}[r["axis"]])
    locked = {lab for r in reqs for lab in [r.get("label"), r.get("other"), *r.get("labels", [])]
              if lab}
    prev = None
    if parent:
        fam = _family(job) if (job / "..").exists() else {}
        pdir = fam.get(parent)
        if pdir and (pdir / "outline.json").exists() and (pdir / "result.json").exists():
            po = json.loads((pdir / "outline.json").read_text())
            prow = {x["label"]: x["value"]
                    for x in json.loads((pdir / "result.json").read_text())["labels"]}
            prev = {"version": parent, "outer": np.array(po["outer"]),
                    "holes": [np.array(h) for h in po["holes"]], "rows": prow}
    drawing.render(shape, f_ext, f_ints, str(dpng), str(dpdf), name=name, version=version,
                   thickness=thickness,
                   requirements=list(dict.fromkeys(refine.req_desc(r) for r in reqs)),
                   locked=locked, prev=prev,
                   finish={k: v for k, v in finish.items() if v[0] != "none"})

    scad = job / f"{name}.scad"
    scad.write_text(to_scad(m, name, source=src.name))

    stl = job / f"{name}.stl"
    log = build.export_stl(str(scad), str(stl))
    val = build.validate(str(stl))
    # Generated files must compile clean; a warning means geometry was
    # silently dropped or changed.
    scad_warn = [ln.strip() for ln in log.splitlines() if "WARNING" in ln]
    if scad_warn:
        val["ok"] = False
        m.warnings += [f"openscad: {w}" for w in scad_warn]

    png = None
    if preview:
        try:
            build.render_preview(str(scad), str(job / "preview.png"))
            png = str(job / "preview.png")
        except Exception as e:          # a preview is nice-to-have, not a failure
            m.warnings.append(f"preview render failed: {str(e).splitlines()[0]}")

    want = [length, width, thickness]
    if any(abs(a - b) > 0.05 for a, b in zip(val["extents_mm"], want)):
        val["ok"] = False
        m.warnings.append(f"STL size {val['extents_mm']} doesn't match the "
                          f"requested {want}")

    r = Result(
        ok=val["ok"], job_dir=str(job), scad=str(scad), stl=str(stl),
        preview=png, overlay=str(ov), drawing=str(dpng), drawing_pdf=str(dpdf),
        length=length,
        width=width, thickness=thickness, layer_height=LAYER_H,
        trace_mode=t.mode, rotated=m.rotated, stretch=round(m.stretch, 3),
        points=len(m.outer) + sum(len(h) for h in m.holes), holes=len(m.holes),
        version=version, symmetry=symmetry, straighten=straighten,
        edits=[list(e) for e in edits], labels=shape.legend(),
        validation=val, warnings=t.warnings + shape.warnings + m.warnings,
        requirements=list(dict.fromkeys(refine.req_desc(r) for r in reqs)),
        locked=sorted(locked), finish={k: list(v) for k, v in finish.items()},
    )
    (job / "result.json").write_text(json.dumps(asdict(r), indent=2))
    (job / "job.json").write_text(json.dumps(dict(
        params=params, symmetry=symmetry, straighten=straighten,
        edits=r.edits, version=version, free_size=list(free_size), parent=parent,
        note=note or (", ".join(refine.edit_text(e) for e in edits[new_from:])
                          if version > 1 else "original")),
        indent=2))
    return r
