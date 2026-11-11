#!/usr/bin/env python3
"""box_wizard.py — interactive wizard to design, generate, and TEST a custom
rugged tool box (engine: vendored smkent/monoscad rugged-box, CC BY-SA 4.0).

Interactive:   python3 scripts/box_wizard.py
Scripted:      python3 scripts/box_wizard.py --yes --width 160 --length 90 \
                   --bottom-height 45 --top-height 15 --latch-count 2 --handle

It asks about dimensions, the top/bottom height split, number of clasps, a
handle, seal/latch style and more, renders every needed part to STL (in
parallel), writes a manifest, then runs the unit-test suite to confirm the box
is watertight, single-body, correctly sized, and that the two halves mate.
"""
import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rbox_common as rc


# --------------------------------------------------------------------------
# small interactive helpers
# --------------------------------------------------------------------------
def _istty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(prompt, default, kind="str", choices=None):
    d = default
    while True:
        sfx = f" [{d}]"
        if choices:
            sfx = f" {list(choices)} [{d}]"
        raw = input(f"  {prompt}{sfx}: ").strip()
        if raw == "":
            return d
        try:
            if kind == "int":
                v = int(raw)
            elif kind == "float":
                v = float(raw)
            elif kind == "bool":
                v = raw.lower() in ("y", "yes", "true", "1")
            elif kind == "choice":
                v = type(default)(raw) if not isinstance(default, str) else raw
                if choices and v not in choices:
                    print(f"    choose one of {list(choices)}"); continue
            else:
                v = raw
            return v
        except ValueError:
            print("    invalid value, try again")


def ask_height_split(params, interactive):
    """Handle the top/bottom depth split specially (equal / percentage / direct)."""
    if not interactive:
        return  # values already set from CLI/defaults
    print("\nTop/bottom height split:")
    mode = ask("Set heights how? 1=equal split, 2=percentage split, 3=enter each",
               1, "int")
    if mode in (1, 2):
        total = ask("Total interior height (bottom + top, mm)",
                    params["Bottom_Height"] + params["Top_Height"], "int")
        pct = 50 if mode == 1 else ask("Percentage that is the BOTTOM (rest is top)",
                                       70, "int")
        params["Bottom_Height"] = round(total * pct / 100)
        params["Top_Height"] = total - params["Bottom_Height"]
        print(f"  -> bottom {params['Bottom_Height']} mm / top {params['Top_Height']} mm")
    else:
        params["Bottom_Height"] = ask("Interior BOTTOM height (mm)",
                                      params["Bottom_Height"], "int")
        params["Top_Height"] = ask("Interior TOP height (mm)",
                                   params["Top_Height"], "int")


# --------------------------------------------------------------------------
# argument parsing (every option is also a CLI flag)
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="Design + generate + test a rugged box")
    for o in rc.OPTIONS:
        flag = "--" + o["key"].lower().replace("_", "-")
        if o["kind"] == "bool":
            p.add_argument(flag, dest=o["key"], action=argparse.BooleanOptionalAction,
                           default=None, help=o["prompt"])
        else:
            if o["kind"] == "choice":
                typ = type(o["default"])          # int choices vs str choices
            else:
                typ = {"int": int, "float": float}.get(o["kind"], str)
            p.add_argument(flag, dest=o["key"], type=typ, default=None,
                           choices=o.get("choices"), help=o["prompt"])
    p.add_argument("--out", default=os.path.join(rc.ROOT, "out"),
                   help="output directory")
    p.add_argument("--jobs", type=int, default=4, help="parallel render workers")
    p.add_argument("--yes", action="store_true",
                   help="non-interactive: use defaults/flags, no prompts")
    p.add_argument("--no-test", action="store_true", help="skip the test suite")
    p.add_argument("--json", action="store_true",
                   help="machine-readable JSON result on stdout (implies --yes; "
                        "for bots/automation)")
    p.add_argument("--check-only", action="store_true",
                   help="validate the options and exit without rendering")
    return p


def collect_params(args):
    interactive = not args.yes and not args.json and _istty()
    params = dict(rc.DEFAULTS)
    provided = {o["key"]: getattr(args, o["key"]) for o in rc.OPTIONS}

    # apply any CLI-provided values first
    for k, v in provided.items():
        if v is not None:
            params[k] = v

    if interactive:
        print("=== Rugged Box Wizard ===\nPress Enter to accept the [default].\n")
        print("Dimensions:")
        for k in ("Width", "Length"):
            o = next(o for o in rc.OPTIONS if o["key"] == k)
            if provided[k] is None:
                params[k] = ask(o["prompt"], params[k], o["kind"])
        ask_height_split(params, interactive)
        print("\nFeatures:")
        for o in rc.OPTIONS:
            k = o["key"]
            if k in ("Width", "Length", "Bottom_Height", "Top_Height"):
                continue
            if provided[k] is not None:
                continue
            if k == "Label_Text" and not params.get("Label"):
                continue
            params[k] = ask(o["prompt"], params[k], o["kind"], o.get("choices"))
    return params, interactive


# --------------------------------------------------------------------------
# generation + testing
# --------------------------------------------------------------------------
def generate(params, outdir, jobs, quiet=False):
    """Render every needed part. Returns (manifest|None, errors:list)."""
    os.makedirs(outdir, exist_ok=True)
    parts = rc.parts_for(params)
    tag = (f"{params['Width']}x{params['Length']}"
           f"x{params['Bottom_Height']}+{params['Top_Height']}")
    files = {p: f"{p}_{tag}.stl" for p in parts}

    _, _, manifold = rc.find_openscad()
    if not quiet:
        print(f"\nRendering {len(parts)} part(s) with OpenSCAD"
              + (" (Manifold, fast)" if manifold
                 else " (CGAL — this can take a few minutes)") + "...")

    t0 = time.time()
    results, failures = {}, []
    with cf.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        fut = {ex.submit(rc.render_part, params, p,
                         os.path.join(outdir, files[p])): p for p in parts}
        for f in cf.as_completed(fut):
            part = fut[f]
            try:
                ok, msg = f.result()
            except Exception as e:  # noqa: BLE001 — report any renderer crash
                ok, msg = False, f"{type(e).__name__}: {e}"
            results[part] = ok
            if not ok:
                failures.append(f"{part}: {msg}")
            if not quiet:
                print(f"  [{'OK ' if ok else 'FAIL'}] {part:14} {files[part]}"
                      + ("" if ok else f"\n      {msg}"))

    if not all(results.values()):
        return None, failures

    manifest = dict(params=params, files=files, parts=parts, tag=tag,
                    latch_count=rc.resolve_latch_count(params),
                    render_seconds=round(time.time() - t0, 2),
                    engine="smkent/monoscad rugged-box (CC BY-SA 4.0)")
    with open(os.path.join(outdir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest, []


def run_tests(outdir, quiet=False):
    """Run the suite against outdir. Returns (passed, output)."""
    env = dict(os.environ, BOX_OUT=outdir)
    if not quiet:
        print("\nRunning unit tests...")
    r = subprocess.run([sys.executable, os.path.join(rc.HERE, "test_box.py")],
                       env=env, capture_output=quiet, text=True)
    out = ((r.stdout or "") + (r.stderr or "")) if quiet else ""
    return r.returncode == 0, out


def print_bom(params, manifest, outdir):
    n_latch = manifest["latch_count"]
    print("\n================ BUILD SUMMARY ================")
    print(f"  Interior : {params['Width']} x {params['Length']} mm, "
          f"bottom {params['Bottom_Height']} mm + top {params['Top_Height']} mm")
    print("  Parts    : " + ", ".join(manifest["parts"]))
    print(f"  Print qty: 1x bottom, 1x top, {n_latch}x latch"
          + (", 1x handle" if params.get("Handle") else "")
          + (", 1x label" if params.get("Label") else ""))
    print("  Hardware : M3 screws for hinges + latches "
          "(M3x40 typical; see tools/monoscad/rugged-box/README.md for the count)")
    print("  Print    : 0.2 mm layers, PLA, no supports (Prusa CORE One)")
    print(f"  Files    : {manifest['tag']}  in  {outdir}  "
          f"({manifest['render_seconds']}s)")
    print("  Engine   : smkent/monoscad rugged-box, CC BY-SA 4.0")
    print("===============================================")


def _emit_json(status, **kw):
    print(json.dumps(dict(status=status, **kw), indent=2))


def main():
    args = build_parser().parse_args()
    quiet = args.json
    params, _ = collect_params(args)

    # ---- pre-flight validation: fail fast with an actionable reason --------
    errors, warnings = rc.validate_params(params)
    if errors:
        if quiet:
            _emit_json("invalid", errors=errors, warnings=warnings, params=params)
        else:
            print("\n✗ Cannot build with these options:")
            for e in errors:
                print(f"    - {e}")
            for w in warnings:
                print(f"    ! {w}")
        sys.exit(3)

    if args.check_only:
        if quiet:
            _emit_json("ok", validated=True, warnings=warnings, params=params,
                       latch_count=rc.resolve_latch_count(params),
                       parts=rc.parts_for(params))
        else:
            print("\n✔ Options are valid.")
            for w in warnings:
                print(f"    ! {w}")
        return

    if not quiet:
        print("\nChosen options:")
        for k in ("Width", "Length", "Bottom_Height", "Top_Height", "Latch_Count",
                  "Latch_Type", "Handle", "Lip_Seal_Type", "Wall_Thickness"):
            print(f"  {k:20} = {params[k]}")
        for w in warnings:
            print(f"  ! {w}")

    manifest, failures = generate(params, args.out, args.jobs, quiet=quiet)
    if not manifest:
        if quiet:
            _emit_json("render_failed", errors=failures, params=params)
        else:
            print("\nGeneration FAILED — see errors above.")
        sys.exit(1)

    passed, test_out = (True, "") if args.no_test else run_tests(args.out, quiet)

    if quiet:
        _emit_json("ok" if passed else "tests_failed",
                   out_dir=os.path.abspath(args.out),
                   manifest=os.path.join(os.path.abspath(args.out), "manifest.json"),
                   files={k: os.path.join(os.path.abspath(args.out), v)
                          for k, v in manifest["files"].items()},
                   latch_count=manifest["latch_count"],
                   render_seconds=manifest["render_seconds"],
                   tests_passed=passed, warnings=warnings,
                   test_output=("" if passed else test_out))
        sys.exit(0 if passed else 2)

    print_bom(params, manifest, args.out)
    if not passed:
        print("\n⚠  Unit tests FAILED — review the box before printing.")
        sys.exit(2)
    print("\n✔  All parts generated and tests passed. Ready to slice.")


if __name__ == "__main__":
    main()
