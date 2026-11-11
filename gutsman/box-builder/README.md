# box-builder

Generate a custom, 3D-printable **rugged tool box** (base + lid + latches, with
optional handle and label) from a handful of parameters — then automatically
**verify the result is printable** before you slice it.

Runs as an interactive wizard, a scriptable CLI with JSON output, or a Python
function. No Fusion 360, no manual CAD.

```bash
python3 scripts/box_wizard.py                       # interactive
python3 scripts/box_wizard.py --yes --width 160 --length 100 \
    --bottom-height 45 --top-height 18 --latch-count 2 --handle
```

---

## 1. Install

```bash
sudo bash scripts/install-openscad-global.sh   # OpenSCAD (system-wide, w/ Manifold)
bash scripts/setup.sh                          # verify everything; fetches the engine
```

`setup.sh` is safe to re-run and tells you exactly what's missing.

| Requirement | Why | Install |
|---|---|---|
| **OpenSCAD 2023+** | the renderer; the Manifold backend makes a box take ~7 s instead of ~5 min | `sudo bash scripts/install-openscad-global.sh` (then `sudo openscad-update` to upgrade) |
| **Python 3** + `trimesh`, `numpy` | the verification suite | `pip install trimesh numpy` |
| **admesh** | slicer-grade printability check | `sudo dnf install admesh` |
| **git** | fetches the geometry engine on first run | `sudo dnf install git` |

> Fedora's repo and Flathub both ship OpenSCAD **2021.01**, which lacks the
> Manifold backend and is ~40× slower. The installer fetches the current
> upstream build into `/opt/openscad` and links it onto `PATH`, so every
> project on the machine gets it.

---

## 2. Options

| Wizard question | Flag | Values |
|---|---|---|
| Interior width (side-to-side) | `--width` | 20–300 mm |
| Interior length (front-to-back) | `--length` | 20–300 mm |
| Interior **base** height | `--bottom-height` | 0–300 mm |
| Interior **lid** height | `--top-height` | 0–300 mm |
| Number of clasps | `--latch-count` | `0` auto, `1`, `2` |
| Latch style | `--latch-type` | `clip`, `draw` |
| Carry handle | `--handle` / `--no-handle` | requires a 2-latch box |
| Lip seal | `--lip-seal-type` | `none`, `wedge`, `square`, `filament-1.75mm` |
| Corner radius | `--corner-radius` | 0–20 mm |
| Reinforced corners | `--reinforced-corners` | |
| Lid front grip | `--top-grip` | 2-latch boxes only |
| Wall / lip thickness | `--wall-thickness` / `--lip-thickness` | 0.4–10 mm |
| Label plate + text | `--label` / `--label-text` | requires a 2-latch box |

The **height split** is how you control base-vs-lid depth: `--bottom-height 45
--top-height 15` gives a deep base with a shallow cover; equal values give a
clamshell. The interactive wizard offers equal / percentage / direct entry.

Other flags: `--out DIR`, `--jobs N`, `--yes` (no prompts), `--no-test`,
`--json`, `--check-only`.

### Feature constraints (enforced before rendering)
The geometry engine only builds a **handle** or **label** on a **2-latch box**
that is large enough. Rather than let that fail mysteriously mid-render,
box-builder asks the engine up front (`probe.scad`, ~40 ms) and refuses with a
plain-English reason. Use `--check-only` to validate without rendering.

---

## 3. Bot / automation integration

### Option A — CLI with JSON (any language)

```bash
python3 scripts/box_wizard.py --json \
    --width 160 --length 100 --bottom-height 45 --top-height 18 \
    --latch-count 2 --handle --out /srv/boxes/job123
```

Success prints JSON to stdout and exits `0`:

```json
{
  "status": "ok",
  "out_dir": "/srv/boxes/job123",
  "manifest": "/srv/boxes/job123/manifest.json",
  "files": {
    "bottom": "/srv/boxes/job123/bottom_160x100x45+18.stl",
    "top":    "/srv/boxes/job123/top_160x100x45+18.stl",
    "latch":  "/srv/boxes/job123/latch_160x100x45+18.stl",
    "handle": "/srv/boxes/job123/handle_160x100x45+18.stl"
  },
  "latch_count": 2,
  "render_seconds": 7.0,
  "tests_passed": true,
  "warnings": []
}
```

**Exit codes** (all paths emit JSON when `--json` is used):

| Code | `status` | Meaning |
|---|---|---|
| `0` | `ok` | Parts generated and verified — safe to slice |
| `1` | `render_failed` | The renderer failed; see `errors[]` |
| `2` | `tests_failed` | Parts exist but failed verification; see `test_output` |
| `3` | `invalid` | Bad/impossible options; see `errors[]` — nothing was rendered |

`--json` implies non-interactive, so it never blocks waiting on input. All
paths in the output are absolute, and it works from any working directory.

### Option B — Python API

```python
import sys; sys.path.insert(0, "/path/to/box-builder/scripts")
from rbox_common import build_box

r = build_box("/srv/boxes/job123",
              Width=160, Length=100, Bottom_Height=45, Top_Height=18,
              Latch_Count=2, Handle=True)

if r["status"] == "ok":
    for part, path in r["files"].items():
        print(part, path)
else:
    print("failed:", r["errors"])
```

`build_box()` never raises for a bad configuration — check `status`. Unknown
option names are rejected (catches typos like `Widht=160`).

### Notes for unattended use
- **Concurrency**: give each job its own `--out` directory; nothing is shared.
- **Runtime**: ~7 s for a typical box; a `draw` latch adds ~20 s. Renders run in
  parallel (`--jobs`, default 4). Allow a 5-minute ceiling per job.
- **Validate first**: `--check-only --json` costs ~40 ms and catches impossible
  configs before you queue a render.
- Strings are escaped, so a user-supplied `--label-text` cannot inject SCAD code.

---

## 4. What gets verified

Every run ends with a test suite (`scripts/test_box.py`); `tests_passed: false`
means don't print it. Run standalone with `BOX_OUT=out python3 scripts/test_box.py`.

- Every expected part exists and is non-empty
- **admesh**: no holes, no disconnected facets (the slicer-grade check)
- No unexpected loose pieces — allowing that a `draw` latch is legitimately two
  pieces (lever + catch), and ignoring the coincident seal-lip flange
- Interior is never shrunk below what was requested
- Outer footprint matches `width + walls` on normal-sized boxes
- Base and lid footprints match, so the halves mate
- Base/lid heights land in the expected range
- Handle/label present **iff** requested

---

## 5. Printing

0.2 mm layers, PLA, no supports (tuned for a Prusa CORE One, 0.4 mm nozzle).
Hinges and latches are held by **M3 screws** — a box needs roughly 6–25
depending on handle and stacking latches. See
`tools/monoscad/rugged-box/README.md` for the exact hardware list.

---

## 6. Layout & attribution

```
scripts/
  box_wizard.py              wizard + CLI (JSON mode for bots)
  rbox_common.py             engine discovery, validation, render, build_box() API
  test_box.py                printability + dimensional test suite
  boxgen.scad                parameter wrapper around the engine
  probe.scad                 asks the engine which features it can build
  setup.sh                   dependency check / engine fetch
  install-openscad-global.sh system-wide OpenSCAD installer
tools/monoscad/              the geometry engine (fetched by setup.sh)
```

Geometry comes from the **[smkent/monoscad](https://github.com/smkent/monoscad)
"rugged-box"** model by smkent / bulbasaur0, licensed **CC BY-SA 4.0** — keep
that attribution on anything you redistribute. This wrapper (wizard,
validation, tests, packaging) is the part built here.

Also in the repo: `scripts/make_box.py` + `build.sh` drive an alternative
**FreeCAD** rugged-box model (`models/*.fcstd`) that only exposes length/width/
height with a fixed M3×30 latch; it needs FreeCAD 1.0 (`tools/freecad/`).
`scripts/lib.scad` and `scripts/box.scad` are superseded from-scratch
experiments, kept for reference only.
