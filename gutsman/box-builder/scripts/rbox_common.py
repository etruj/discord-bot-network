#!/usr/bin/env python3
"""rbox_common.py — shared plumbing for the rugged-box wizard and its tests.

Wraps the vendored smkent/monoscad rugged-box OpenSCAD engine
(tools/monoscad/rugged-box, CC BY-SA 4.0): engine discovery, part rendering,
the option schema, and the dimension formulas the tests assert against.
"""
import functools
import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIB_DIR = os.path.join(ROOT, "tools", "monoscad", "rugged-box")
BOXGEN = os.path.join(HERE, "boxgen.scad")

# Engine defaults for boxgen.scad variables that the wizard does not prompt for.
# Validation needs these to predict the engine's own feature gates.
ENGINE_DEFAULTS = {"Latch_Width": 28, "Rib_Width": 6, "Size_Tolerance": 0.20}

# Hard limits. The engine's customizer allows 20..300 mm per dimension; going
# outside that produces broken or absurd geometry.
LIMITS = {
    "Width": (20, 300), "Length": (20, 300),
    "Bottom_Height": (0, 300), "Top_Height": (0, 300),
    "Corner_Radius": (0, 20), "Wall_Thickness": (0.4, 10),
    "Lip_Thickness": (0.4, 10),
}

# ---- option schema -------------------------------------------------------
# Each option: key = boxgen.scad variable, kind, default, prompt, choices.
# kind: "int" | "float" | "bool" | "str" | "choice"
OPTIONS = [
    dict(key="Width",  kind="int",   default=180, prompt="Interior width (side-to-side, mm)"),
    dict(key="Length", kind="int",   default=90,  prompt="Interior length (front-to-back, mm)"),
    dict(key="Bottom_Height", kind="int", default=40, prompt="Interior BOTTOM height (mm)"),
    dict(key="Top_Height",    kind="int", default=20, prompt="Interior TOP/lid height (mm)"),
    dict(key="Latch_Count", kind="choice", default=0, choices=[0, 1, 2],
         prompt="Number of clasps/latches (0 = auto)"),
    dict(key="Latch_Type", kind="choice", default="clip", choices=["clip", "draw"],
         prompt="Latch style"),
    dict(key="Handle", kind="bool", default=False, prompt="Add a carry handle?"),
    dict(key="Lip_Seal_Type", kind="choice", default="wedge",
         choices=["none", "wedge", "square", "filament-1.75mm"],
         prompt="Lip seal type"),
    dict(key="Corner_Radius", kind="float", default=5.0, prompt="Corner radius (mm)"),
    dict(key="Reinforced_Corners", kind="bool", default=False, prompt="Reinforced corners?"),
    dict(key="Top_Grip", kind="bool", default=False, prompt="Front grip on the lid (2-latch boxes)?"),
    dict(key="Wall_Thickness", kind="float", default=3.0, prompt="Wall thickness (mm)"),
    dict(key="Lip_Thickness", kind="float", default=3.0, prompt="Extra lip thickness (mm)"),
    dict(key="Label", kind="bool", default=False, prompt="Add a label plate?"),
    dict(key="Label_Text", kind="str", default="Label", prompt="Label text"),
]
STRING_KEYS = {"Latch_Type", "Lip_Seal_Type", "Label_Text", "Part"}
BOOL_KEYS = {o["key"] for o in OPTIONS if o["kind"] == "bool"}
DEFAULTS = {o["key"]: o["default"] for o in OPTIONS}


def parts_for(params):
    """Which part STLs to generate for the chosen options."""
    parts = ["bottom", "top", "latch"]
    if params.get("Handle"):
        parts.append("handle")
    if params.get("Label"):
        parts.append("label")
    return parts


def _eng(params, key):
    return float(params.get(key, ENGINE_DEFAULTS[key]))


PROBE = os.path.join(HERE, "probe.scad")


def probe(params):
    """Ask the ENGINE which optional features it will actually build.

    The handle/label gates (and the auto latch count) depend on derived values
    inside rugged-box-library.scad; re-deriving them in Python would silently
    drift when the engine is updated. probe.scad evaluates the real predicates
    and echoes them — it renders no geometry and takes ~40 ms.

    Returns a dict like {'latch_count': 2, 'handle': True, 'label': False,
    'outer_width': 172.0, 'outer_length': 112.0}, or {} if the probe fails
    (callers then fall back to rendering and reporting whatever happens).
    """
    argv, env, _ = find_openscad()
    cmd = list(argv) + ["--export-format", "echo", "-o", "-"]
    cmd += scad_defines(dict(params))
    cmd.append(PROBE)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           env=env)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    line = next((l for l in ((p.stdout or "") + (p.stderr or "")).splitlines()
                 if "PROBE " in l), None)
    if not line:
        return {}
    out = {}
    for tok in line.split("PROBE ", 1)[1].strip().strip('"').split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if v in ("true", "false"):
            out[k] = (v == "true")
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    if "latch_count" in out:
        out["latch_count"] = int(out["latch_count"])
    return out


def resolve_latch_count(params):
    """1 or 2 (0 = auto). Uses the engine probe, with a formula fallback."""
    lc = int(params.get("Latch_Count", 0))
    if lc in (1, 2):
        return lc
    info = probe(params)
    if info.get("latch_count") in (1, 2):
        return info["latch_count"]
    outer_radius = float(params["Corner_Radius"]) + float(params["Wall_Thickness"])
    return 2 if float(params["Width"]) >= (outer_radius * 2
                                           + _eng(params, "Latch_Width") * 4) else 1


def validate_params(params):
    """Pre-flight check: catch configurations the engine cannot build BEFORE
    spending time rendering. Returns (errors, warnings) as lists of strings.

    These mirror gates inside rugged-box-library.scad (_handle_enabled,
    _label_enabled, _compute_latch_count). Rendering a gated-off part yields
    OpenSCAD's cryptic "Current top level object is empty" and a missing file,
    so we explain it up front instead.
    """
    errors, warnings = [], []

    for key, (lo, hi) in LIMITS.items():
        if key not in params:
            continue
        try:
            v = float(params[key])
        except (TypeError, ValueError):
            errors.append(f"{key} must be a number (got {params[key]!r})")
            continue
        if not (lo <= v <= hi):
            errors.append(f"{key} must be between {lo} and {hi} mm (got {v:g})")

    if float(params.get("Bottom_Height", 0)) + float(params.get("Top_Height", 0)) <= 0:
        errors.append("Bottom_Height + Top_Height must be greater than 0")

    if float(params.get("Corner_Radius", 0)) > min(float(params.get("Width", 1e9)),
                                                   float(params.get("Length", 1e9))) / 2:
        errors.append("Corner_Radius is too large for the box footprint")

    if errors:                      # don't probe a box the engine can't parse
        return errors, warnings

    # ---- authoritative feature gates, straight from the engine -------------
    info = probe(params)
    if not info:
        warnings.append("Could not probe the engine for feature availability; "
                        "proceeding — a disabled feature will fail at render.")
        return errors, warnings

    lc = info.get("latch_count")
    auto = int(params.get("Latch_Count", 0)) not in (1, 2)

    if params.get("Handle") and not info.get("handle"):
        why = ("this box resolves to 1 latch"
               + (" (auto — widen the box or set Latch_Count=2)" if auto
                  else " (Latch_Count=1)")) if lc != 2 else \
              ("the box is too short or too narrow for the handle "
               f"(bottom height {params['Bottom_Height']} mm) — a taller "
               "bottom or wider box is needed")
        errors.append(f"Handle cannot be built: {why}. The engine requires a "
                      "2-latch box with room for the handle.")

    if params.get("Label") and not info.get("label"):
        errors.append("Label cannot be built: the engine requires a 2-latch box "
                      "with a large enough front face for the label holder.")

    if params.get("Top_Grip") and lc != 2:
        warnings.append("Top_Grip only applies to 2-latch boxes; it will be ignored.")

    return errors, warnings


# ---- dimension formulas (mirror rugged-box-library.scad) ------------------
def total_lip(params):
    return float(params["Wall_Thickness"]) + float(params["Lip_Thickness"])


def outer_width(params):
    return float(params["Width"]) + 2 * total_lip(params)


def outer_length(params):
    return float(params["Length"]) + 2 * total_lip(params)


# ---- engine discovery -----------------------------------------------------
def _supports_manifold(argv, env):
    """True if this OpenSCAD accepts --backend=manifold (2023+; ~10x faster)."""
    try:
        h = subprocess.run(argv + ["--help"], capture_output=True, text=True,
                           env=env, timeout=60)
        return "backend" in (h.stderr or "") + (h.stdout or "")
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def find_openscad():
    """Return (argv_prefix, env, supports_manifold). Cached — probing `--help`
    per part would otherwise cost an extra subprocess for every render.

    Order: a globally-installed Manifold-capable OpenSCAD, the repo-bundled
    copy, an old system CGAL build, then Flatpak."""
    env = dict(os.environ, OPENSCADPATH=LIB_DIR)
    # 1) A globally-installed modern OpenSCAD (Manifold backend) is preferred —
    #    every project benefits and it stays current. See README for the install.
    sys_oscad = shutil.which("openscad")
    if sys_oscad and _supports_manifold([sys_oscad], env):
        return [sys_oscad], env, True
    # 2) Otherwise the fast copy bundled in this repo.
    bundled = os.path.join(ROOT, "tools", "openscad", "usr", "bin", "openscad")
    if os.path.exists(bundled):
        return [bundled], env, _supports_manifold([bundled], env)
    # 3) Otherwise an old system OpenSCAD (slow CGAL) — still correct, just slow.
    if sys_oscad:
        return [sys_oscad], env, False
    if shutil.which("flatpak") and subprocess.run(
            ["flatpak", "info", "org.openscad.OpenSCAD"],
            capture_output=True).returncode == 0:
        argv = ["flatpak", "run", "--filesystem=host",
                f"--env=OPENSCADPATH={LIB_DIR}", "org.openscad.OpenSCAD"]
        return argv, dict(os.environ), _supports_manifold(argv, dict(os.environ))
    raise RuntimeError("No OpenSCAD found — see README for the global install.")


def build_box(out_dir, run_tests=True, jobs=4, **options):
    """Programmatic API — generate a box in one call. For bot/service use.

        from rbox_common import build_box
        r = build_box("/tmp/box1", Width=160, Length=100,
                      Bottom_Height=45, Top_Height=18, Latch_Count=2, Handle=True)
        if r["status"] == "ok": print(r["files"])

    Returns a dict with 'status' in {ok, invalid, render_failed, tests_failed}
    plus 'files', 'errors', 'warnings', 'params', 'latch_count'. Never raises
    for an invalid configuration — inspect 'status'.
    """
    import box_wizard  # local import: box_wizard imports this module

    params = dict(DEFAULTS)
    unknown = set(options) - set(params)
    if unknown:
        return dict(status="invalid", errors=[f"unknown option(s): "
                                              f"{', '.join(sorted(unknown))}"],
                    warnings=[], params=params, files={})
    params.update(options)

    errors, warnings = validate_params(params)
    if errors:
        return dict(status="invalid", errors=errors, warnings=warnings,
                    params=params, files={})

    manifest, failures = box_wizard.generate(params, out_dir, jobs, quiet=True)
    if not manifest:
        return dict(status="render_failed", errors=failures, warnings=warnings,
                    params=params, files={})

    files = {k: os.path.join(os.path.abspath(out_dir), v)
             for k, v in manifest["files"].items()}
    passed, test_out = (True, "")
    if run_tests:
        passed, test_out = box_wizard.run_tests(out_dir, quiet=True)
    return dict(status="ok" if passed else "tests_failed", errors=[],
                warnings=warnings, params=params, files=files,
                latch_count=manifest["latch_count"],
                render_seconds=manifest["render_seconds"],
                tests_passed=passed, test_output="" if passed else test_out,
                manifest=os.path.join(os.path.abspath(out_dir), "manifest.json"))


def _scad_str(s):
    """Quote a Python string as an OpenSCAD string literal. Escaping matters:
    a label containing a quote or backslash would otherwise corrupt the -D
    expression (or inject SCAD code when the caller is a bot)."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def scad_defines(params):
    args = []
    for k, v in params.items():
        if k in BOOL_KEYS or isinstance(v, bool):
            args += ["-D", f"{k}={'true' if v else 'false'}"]
        elif k in STRING_KEYS or isinstance(v, str):
            args += ["-D", f"{k}={_scad_str(v)}"]
        else:
            args += ["-D", f"{k}={v}"]
    return args


# An STL smaller than this is an empty/degenerate export, not a real part.
MIN_STL_BYTES = 200


def render_part(params, part, out_path, timeout=900):
    """Render one part to a binary STL. Returns (ok, message).

    Removes any stale output first so a previous run's file can never be
    mistaken for a fresh success, and translates OpenSCAD's cryptic
    "Current top level object is empty" into an actionable explanation.
    """
    out_path = os.path.abspath(out_path)
    try:
        os.remove(out_path)
    except FileNotFoundError:
        pass

    argv, env, manifold = find_openscad()
    cmd = list(argv) + ["--export-format", "binstl"]
    if manifold:
        cmd += ["--backend=manifold"]
    cmd += scad_defines(dict(params, Part=part))
    cmd += ["-o", out_path, os.path.abspath(BOXGEN)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, (f"render timed out after {timeout}s"
                       + ("" if manifold else " (slow CGAL backend — install a "
                                              "Manifold-capable OpenSCAD)"))

    msg = ((proc.stderr or "") + (proc.stdout or "")).strip()
    exists = os.path.exists(out_path)
    size = os.path.getsize(out_path) if exists else 0

    if "top level object is empty" in msg.lower():
        return False, (f"the engine produced no geometry for '{part}' — this "
                       "feature is disabled for the chosen options (a handle "
                       "and label both require a 2-latch box of sufficient "
                       "size). Adjust the options and retry.")
    if proc.returncode != 0:
        return False, msg or f"OpenSCAD exited {proc.returncode}"
    if not exists or size < MIN_STL_BYTES:
        return False, f"export produced an empty STL ({size} bytes) for '{part}'"
    return True, msg
