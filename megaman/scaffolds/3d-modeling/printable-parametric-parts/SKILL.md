---
name: printable-parametric-parts
description: >-
  Rules and workflow for designing 3D-printable parts as parametric OpenSCAD
  code and exporting slicer-ready STLs. Use this skill whenever the user wants
  to model, modify, or export a physical part for FDM 3D printing — including
  any mention of OpenSCAD, .scad, STL, "3D print", enclosures, brackets,
  fittings, snap/slide/dovetail joints, tolerances/clearances, or "the model
  won't load in my slicer". Also use it whenever the user wants to copy,
  replicate, reverse-engineer, or remix an existing physical object from
  photos, traces, or a sketch — it defines the colour-coded measurement-request
  loop for pinning down the dimensions you can't see. Apply it even when the request sounds simple (e.g.
  "make this thinner", "add a logo", "round the edges"), because the
  printability, fit-tolerance, mesh-validation, and binary-export rules here
  prevent parts that look fine on screen but fail to print or fit.
---

# Printable Parametric Parts (OpenSCAD → STL)

A discipline for turning ideas into parts that actually print and actually fit,
on the first or second try. Geometry is written as parametric OpenSCAD code,
verified with renders and cross-sections, gated through mesh validation, and
shipped as binary STL.

## Golden rules (read first)

1. **Export binary STL, never ASCII.** ASCII STL can contain
   scientific-notation coordinates that some slicers reject as "corrupt." Use
   `--export-format binstl`. If a user says a file "won't load," suspect format
   before geometry.
2. **A validated joint is frozen.** Once a fit (dovetail, snap, press-fit,
   clearance) is confirmed by the user to work, its dimensions and clearance do
   **not** change when you do unrelated work. Re-orienting, thinning, or
   restyling must preserve the proven numbers exactly.
3. **Verify by rendering before shipping.** Render an overall preview *and*
   cross-sections of every internal feature (pockets, grooves, caps, clearances)
   and look at them. "Compiles" is not "correct."
4. **Validate the mesh every export.** Single connected solid, watertight, no
   slivers. Details below. A clean-looking model can still be non-manifold.
5. **Design for the print, not just the screen.** Orientation, overhangs,
   bridges, and minimum wall/cap thicknesses are part of the design, not an
   afterthought.
6. **Stamp a discreet version mark every revision.** Engrave `v#` on a
   hidden/inner face of each part — once per part, on solid material, never over
   a cutout. It is the only reliable way to tell printed iterations apart later;
   trivial to add, genuinely painful to lack. Treat a missing or duplicated
   version mark as a bug to fix before shipping.
7. **Coincident faces corrupt booleans — even when trimesh says "watertight."**
   OpenSCAD's CGAL "The given mesh is not closed!" almost always means two faces
   sit on the exact same plane (a frame cap meeting an opener at the same line, a
   cut boundary flush with a wall, a clip whose tongue edge coincides with the
   slot wall). The exported soup can still pass a watertight check yet render
   wrong / slice wrong. Fix the *coincidence*: run walls tall and cut with an
   OVERLAPPING tool, or grow a clip/cut ~0.3 mm into the empty gap so nothing is
   ever co-planar. Don't ship a part that throws this error.
8. **Round corners with geometry, not stamps.** To round a corner, build the
   real arc (explicit polygon, or `hull()` of circles) — do **not** drop a
   filled circle onto it (reads as a blob) and do **not** reach for
   `offset(+r) offset(-r)` on thin features (the shrink collapses slot walls and
   destroys the mesh). To square *one* end of an otherwise-rounded shape, extend
   it past the line and clip with a plain rectangle.
9. **Reverse-engineering: ask with colour, not prose.** When copying an existing
   object, never guess silently and never ask "how thick is it?" in words. Render
   the model in grey with every unknown drawn on it as a **coloured, numbered
   marker**, send the image, and let the user answer by number. Full loop in
   "Reverse-engineering an existing object" below.
10. **Set `$fa`/`$fs` inside the part module.** They are dynamically scoped and
    do not cross a `use <>` boundary, so file-scope values silently vanish when
    a wrapper (export script, measure view, remix file) calls the module — the
    STL comes out at OpenSCAD's coarse 12°/2 mm defaults. Check the facet count
    of the export matches a direct render.

## Toolchain

| Tool | Role |
|------|------|
| OpenSCAD (CLI) | parametric modelling, STL export, preview renders |
| admesh | slicer-grade STL validation/repair (PrusaSlicer uses it internally) |
| trimesh (Python) | mesh diagnostics: watertight, body count, sliver detection |
| potrace + Pillow (Python) | raster logo → tight PBM → vector SVG |
| matplotlib (Python) | dimensioned 2D engineering drawings |
| opencv-python + shapely (Python) | feature map: outline tracing and geometry (`feature_map.py`) |
| xvfb | only needed for headless/CI rendering; not on a desktop |

Fedora setup (one line):
```bash
sudo dnf install -y openscad admesh potrace python3 python3-pip && \
  pip3 install --user trimesh Pillow matplotlib numpy opencv-python shapely
```
Debian/Ubuntu: swap `dnf install` for `apt-get install`.

## Reusable code (scripts/)

Start from working code, not a blank file. Bundled in `scripts/`:

- **`lib.scad`** — tested OpenSCAD modules: `pp_rrect`, `pp_rounded_body`
  (all-edge fillet via minkowski), `pp_dovetail_x` / `pp_dovetail_y` (groove
  cutters), `pp_tongue_y` (mating tongue with lead-in ramps + clearance +
  embed), `pp_engrave` (version/text), `pp_deboss_svg` (logo), and the
  cantilever-clip family `pp_clip_shape` / `pp_clip_uslot2d` / `pp_clip_taper`
  (a leaf-spring page-clip tongue: rounded bottom corners, square full-thickness
  hinge, wedge taper, coincidence-free slot) plus `pp_cut_through` (cut a
  profile clean through a part so a second-colour layer shows through — used for
  the through-cut logo). Recommended FDM constants live at the top
  (`PP_FIT_CLEAR=0.10`, `PP_EDGE_FILLET=0.30`, `PP_EMBED=0.20`).
  `use <lib.scad>` — it defines no top-level geometry.
- **`example.scad`** — a runnable two-part dovetail sandwich (window, side
  joint, engraved version) showing the modules composed correctly. Copy it as a
  starting point.
- **`export_and_validate.sh`** — `./export_and_validate.sh model.scad Module
  out.stl` exports a part to **binary** STL (auto-headless via xvfb) and runs
  validation. One command per part.
- **`validate_stl.py`** — the validation gate: binary-format, watertight,
  single-body, sliver (advisory), and admesh holes. Exit 0 = pass.
- **`trace_logo.py`** — `python3 trace_logo.py in.png out.svg` does
  threshold → tight crop → PBM → potrace → SVG, ready for `pp_deboss_svg`.
- **`stamp_logo.py`** — bakes a logo onto **any** STL (not just scad parts):
  `python3 stamp_logo.py part.stl` debosses `assets/logo.svg` on the bottom
  (visible) face, auto-mirrored, then validates. Flags: `--face top|bottom`,
  `--size`, `--depth`, `--mode deboss|emboss`, `--at X,Y`, `--logo`. Uses
  OpenSCAD for the boolean, so SVG holes just work.
- **`make_drawings.py`** — dimensioned drawing sheets (front plan, back plan,
  dovetail detail + parameter table) → PDF + PNGs. Edit the `PARAMS` dict or
  pass a JSON of the same keys: `python3 make_drawings.py mypart.json out_dir`.
- **`feature_map.py`** — the **labelled engineering drawing** (feature map):
  `python3 scripts/feature_map.py part.stl drawings/` (or `part.scad --module
  lid`). Every edge, corner and hole gets a balloon (E# / C# / H#) and a row of
  exact geometry, so the user can ask for changes by label. Same code and
  quality as ProtoMan's drawings. See "Feature map" below.
- **`feature_map_engine/`** — the vendored drawing code behind `feature_map.py`
  (copied from ProtoMan; see its `VERSION`). Never edit it in a project.
- **`measure_lib.scad`** — measurement-request markers for reverse-engineering:
  `pp_mark_ghost` (grey model), `pp_mark_bar` (length/thickness),
  `pp_mark_ring` (hole/boss diameter), `pp_mark_path` (curve/profile),
  `pp_mark_region` (paint a whole feature: "real or artefact?"),
  `pp_mark_tag` (camera-facing number/word). Each takes the item number first
  and picks its colour from a fixed palette (1 red, 2 blue, 3 green, 4 orange,
  5 magenta, 6 cyan, 7 yellow, 8 brown).
- **`measure_example.scad`** — runnable four-question measurement request;
  copy it as the starting point for a project's `measure.scad`.
- **`render_measure.sh`** — `./render_measure.sh measure.scad out.png [RX RZ]`
  renders the request view and feeds the same camera angles to the tags, so
  labels always face the camera. Run a second angle for hidden markers.
- **`assets/logo.svg`** — the user's logo, traced and ready; the default for
  `stamp_logo.py` and `pp_deboss_svg`. Replace it to change the brand mark.

## Workflow

1. **Parameterize.** Put every dimension as a named variable at the top; compute
   derived values from them. Group and clearly label any **validated joint
   parameters** with a "do not change once confirmed" comment.
2. **Read the skill first** (this one) before writing geometry. For logos, see
   the logo pipeline below.
3. **Model** with small, named modules (one per part, plus reusable feature
   modules).
4. **Render & inspect**: overall view + cross-sections of internal features.
5. **Export binary STL** for each part separately (one part per file).
6. **Validate** with OpenSCAD's CGAL summary + admesh + trimesh.
7. **Present** the STLs, and offer drawings if useful.
8. **Iterate by version.** Each revision bumps `v#`; engrave it on a hidden
   face (see Versioning).

## Feature map (labelled engineering drawing)

An optional, standard drawing every OpenSCAD project can produce: a
Letter-landscape top view with the part filled, overall dimensions, the
origin, ℄ mirror lines, and a **balloon on every feature**: **E#** edges (blue
boxes), **C#** corners (red circles), **H#** holes (green circles). A table
gives each one's exact geometry ("Y = 99.7;  X 265.0 → 318.4  (53.4 long)",
"R10.0, center (…)", "Ø6.0 thru;  center (…)"). It is the same drawing ProtoMan
posts for his parts, generated by the same code, so every project's drawings
look and read the same. It isn't required on every export; use it when it
helps:

- the user wants to discuss or change geometry ("which edge?", "move that hole")
- signing off a reverse-engineered copy, alongside the measurement round
- the user asks for a drawing, or a change touched several features at once

```bash
python3 scripts/feature_map.py out/part.stl drawings/v3 --version 3
python3 scripts/feature_map.py part.scad drawings/lid --module lid     # exports first
python3 scripts/feature_map.py out/part.stl drawings/v3 --symmetry lr  # label one side
python3 scripts/feature_map.py out/part.stl drawings/pocket --z 2.4    # slice at Z=2.4
```
Writes `feature_map.png` + `.pdf` and `features.json` (every label with kind,
description and value, the size, and `origin_in_model`).

**Reading it**
- Default view = the top **silhouette**: outline + through-holes. Blind pockets,
  slots and bosses only show in a **section** (`--z <height>`); draw one per
  level that matters.
- Origin (0,0) is the part's bottom-left. Add `origin_in_model` (also printed
  in the notes line) to a table value to get the .scad coordinate.
- Symmetric parts: pass `--symmetry lr|tb|both`; only one side is ballooned and
  partners (E#′) change together, so edit the shared parameter.
- Values are measured from the **exported mesh**. A hole that reads a little
  under its nominal size (Ø18.9 for `d = 19`) is facet loss: `$fn`/`$fa`/`$fs`
  are too coarse (golden rule 10). Fix the faceting, not the number.
- One body per drawing: for an assembly, run it per part (`--module`).
- Exact to 0.1 mm for parts up to ~400 mm long; beyond that the raster gets
  coarser (a note says so if islands are dropped).

**Editing by label** — when the user answers "E3 up 2, H1 Ø6, C2 R5":
1. Map each label to the parameter that drives it, using the table row (its
   position / length / radius / centre) against the .scad variables.
2. Change the parameters (never hard-code a one-off offset), re-export,
   re-validate.
3. Regenerate the map with the next `--version` into its own folder, **Read the
   PNG** so it shows inline in the Claude app, and say which numbers changed.
Labels can renumber when the outline changes shape (a new edge appears), so
always work from the newest drawing.

**Quality bar** — don't restyle or hand-draw this sheet, and don't edit
`feature_map_engine/`; improvements go into ProtoMan's engine and come back
through `claude-pm/tools/sync_feature_map.sh`. Look at every map you send: all
balloons readable, no feature missing, and the key values agree with the .scad
(within facet loss). `make_drawings.py` is still the tool for multi-view
dimensioned sheets with joint details; the feature map is the labelled top view.

## Reverse-engineering an existing object

When the user wants a copy of a physical thing (photos, a pencil trace, a few
hand-written numbers), the job is a **conversation in measurement rounds**, not
a one-shot model. Learned on the battery-mount block: the first photo-derived
model was 1.6 mm off on thickness and 4 mm off on an edge, had six "holes" that
were injection-mould marks and a "counterbore" that was a water ring — all
caught in one coloured measurement round instead of a failed print.

### 1. Gather and rank the evidence
- A **1:1 trace** is dimensionally trustworthy; use it for the outline and as
  the scale reference. Photos give proportions and features, not sizes.
- Get a scale (px/mm) off a known dimension in each photo, then derive the
  rest. Cross-check every derived number at least two independent ways.
- **Photogrammetry runs long near the frame edge and on grazing faces** — the
  silhouette picks up the adjoining side face catching light. Any dimension
  pulled from near a photo's edge is suspect by several mm; say so.
- Look at *every* face. A feature on the front that is missing on the back
  photo is blind, not through.

### 2. Tag confidence on every parameter
Every dimension at the top of the `.scad` carries a tag, and the header
explains the key:
```
LEN     = 74;    // [M] measured by the user (calipers / written on the trace)
TOP_W   = 35.1;  // [P] photogrammetry — needs calipers
HOLE_D  = 2.8;   // [?] guess — feature itself may not be real
```
Anything not `[M]` is a candidate for the next measurement round.

### 3. Build the model anyway, then render the measurement request
Model the whole part from the best estimates (fully parametric, one module per
feature) and export a provisional STL so the user can see the direction. Then
write `measure.scad`:
```openscad
use <mypart.scad>
use <scripts/measure_lib.scad>
pp_mark_ghost() mypart();                               // grey body
for (s=[-1,1]) pp_mark_bar(1, [s*(LEN/2+3), 30, 0], [s*(LEN/2+3), 30, THK]);
pp_mark_tag(1, [LEN/2+11, 30, THK/2]);                  // 1 red: thickness
pp_mark_ring(2, [0, SLOT_CY, THK+0.5], RECESS_D/2);     // 2 blue: recess Ø
pp_mark_region(3) hole_cutters();                       // 3 green: real holes?
```
and render it: `./scripts/render_measure.sh measure.scad assets/measure_request.png`.
Then **look at the PNG** — every marker visible, no tag overlapping its own
marker or another tag, nothing hidden behind the part (render a second angle if
so, e.g. `62 202` for the back).

Marker rules:
- **One colour + one number per question**, assigned in palette order. Keep the
  palette mapping stable so "the orange one" is unambiguous.
- Float markers **just outside** the surface they measure (2–4 mm off), and
  mark thicknesses **at both ends** so they read from any angle.
- Use the marker that matches the question: bar = a length, ring = a
  diameter, path = a curve's *shape*, region = "is this a real feature?".
- Ask about *existence* as well as size. Moulding marks, sprue witnesses,
  water rings and wear read as features in photos.
- Ask everything you need in **one round** (5–8 items). Order by impact.

### 4. Ship the image with a matching numbered list
The reply carries the PNG and a list in the same numbers and colours, each
with what you currently assume and what exactly to measure:
```
1 (red)     overall thickness, back face to front face — I have 24.4
2 (blue)    the six small holes: diameter + depth — or are they mould marks?
3 (green)   ring around the slot: diameter + depth of the recess
```
The user answers `1. 26mm  2. remove  3. water ring, remove`.

### 5. Apply, re-tag, log
- Change each answered parameter to `[M]`, delete features the user ruled out,
  re-export, re-validate. Re-check derived geometry: a hard-measured number can
  break a relationship you inferred (a "tangent quarter-circle" cove became a
  quarter-ellipse once both its ends were measured).
- Record in the project LOG which numbers changed, by how much, and *why the
  estimate was wrong* — that bias applies to the remaining `[P]` values.
- Another round if needed. Once the copy is signed off, **repurpose
  `measure.scad` to highlight the feature that's up for redesign** (same
  markers, words instead of numbers: `pp_mark_tag(5, p, "TAB")`) so the scope
  of the next change is unambiguous.

### 6. Remixes keep the copy intact
When the user asks for a variant ("same, but with a half-inch block before the
tab"), thread a parameter through the existing modules with a default that
reproduces the carbon copy exactly (e.g. `sp = 0`), write the variant as its
own small file that calls the module with the new value, and regression-check
that the default still produces the original extents. Never fork the geometry,
and touch nothing the user didn't name.

## FDM fit & tolerance rules

- **Default clearance is 0.1 mm/side.** On a 0.4 mm nozzle, **~0.1 mm/side** is
  the standard sliding/removable-fit gap — enough to move without binding, tight
  enough to stay located. Start here; open it up only if a specific fit actually
  binds (and note the deviation). A near-zero gap can wedge and *delaminate layers
  on insertion*, so don't go below ~0.1 mm.
- **Clearance is applied to the moving part**, by shrinking it, not by growing
  the cavity (keeps outer dimensions stable).
- **Lead-ins.** Give sliding features a ramped/chamfered entry (~3 mm) so they
  self-guide instead of catching.
- **Insert pockets match the insert.** A pocket far deeper than the insert lets
  it rattle and bow. Size the pocket close to the insert thickness (e.g. ~0.2 mm
  for thin thermal paper) to pin it flat; close the pocket on the retaining side
  so the insert can't fall out.
- **Never tighten a validated joint to win some other goal** (e.g. shrinking the
  part). If width/size fights the joint, say so and offer a layout change
  instead of compromising the fit.

## Printability rules

- **Pick an orientation per part and design to it.** State it ("front face down,
  tongues up").
- **Overhangs** on unsupported features should stay within ~45° of vertical.
  Dovetail undercuts printed mouth-down are fine at typical angles.
- **No large flat bridges.** A thin plate spanning between raised features (e.g.
  a plate resting only on two tongue lines) will sag — avoid that orientation.
- **Raised features only on the up-face.** A visible logo on the *bed* face must
  be **recessed (debossed)**, not raised — a raised feature there can't print,
  and flipping the part to raise it usually breaks the joint orientation.
- **Minimum thicknesses (0.4 mm nozzle):** solid flat plate ≳ 0.6–0.8 mm; cap of
  material over a groove ≳ 0.5 mm. Below these, parts get floppy or fragile.
- **Make thicknesses layer-height-friendly.** Choose values that are clean
  multiples of the user's common layer heights (e.g. 1.4 mm = 7×0.2 = 5×0.28).
- **Engraving/text depth** ~0.3 mm; **stroke width ≥ nozzle width** or it won't
  render; verify text **lands on solid material** (a through-window or cutout
  can silently erase it — check the coordinates against the cutout extents).

## Feature fusion (manifold safety)

- **Embed protruding features ~0.2 mm into the parent body** before `union()`.
  A tongue/boss that merely touches the surface on a coincident plane can fail
  to fuse, leaving disconnected bodies (non-manifold / multiple volumes).
- After union, the part must be **one connected solid**.

## Edge rounding

- Round all *exposed* edges (perimeter, faces, window/holes) — a
  minkowski-with-sphere of the (offset-shrunk) profile rounds every edge cleanly,
  including holes.
- **Keep functional mating surfaces crisp.** Never round dovetail/sliding/seating
  surfaces — rounding changes the fit. Round what you touch; leave the joint
  sharp.
- Minkowski is slow and can emit sliver triangles — validate after (see below).
- Fillet radius can't exceed half the local thickness (a 0.8 mm plate maxes near
  0.3–0.4 mm).

## Mesh validation gate (every export)

Run all three; all must pass before presenting a file.

1. **OpenSCAD CGAL summary** (printed on render): want `Simple: yes` and a
   volume count consistent with one solid per part (a simple solid reports
   `Volumes: 2` = the solid + outside; a part with one through-hole reports an
   Euler characteristic of 0, which is correct for a frame).
2. **admesh**: 0 disconnected facets, 0 backwards edges, 0 normals fixed, "no
   holes need to be filled."
   ```bash
   admesh part.stl | grep -E "disconnected|Backwards|Normals fixed|holes"
   ```
3. **trimesh**: `is_watertight == True`, `body_count == 1`, and check for
   **sliver triangles** (very high max edge-aspect ratio). Slivers + ASCII
   format are the usual cause of "won't load" even when topology is fine.
   ```python
   import trimesh, numpy as np
   m = trimesh.load("part.stl")
   print(m.is_watertight, m.body_count, np.round(m.extents,2))
   ```

If validation fails: prefer fixing the *source geometry* (embed features, adjust
booleans) over post-hoc mesh repair. Re-export binary and re-validate.

## Logo / raster-art pipeline

```bash
# 1. PNG → tight black/white PBM (Pillow): grayscale, threshold, crop to bbox
# 2. trace to vector
potrace logo.pbm -s --flat -o logo.svg
```
In OpenSCAD: `resize([d,d]) import("logo.svg", center=true)`, then
`linear_extrude` and **difference** to deboss.
- **Mirror (X) any art/text placed on a face viewed from the back** (the bed/
  outer face), so it reads correctly to the viewer.
- Use a **relative** SVG path in shipped `.scad` files and ship the SVG with
  them; absolute container paths won't resolve on the user's machine.

## Versioning & deliverables

- **Bump `v#` every revision.** Keep both halves of an assembly on the same
  version.
- **Engrave the version on a hidden/inner face, once per part.** Verify it's not
  clipped by a window/cutout (place it on solid material, e.g. a border band).
- When asked for source, ship the **main `.scad` + per-part export wrappers +
  any imported assets (SVG)**. When asked for drawings, generate dimensioned
  multi-view sheets (matplotlib → PDF + PNG) with: plan views, sections, a joint
  detail, and a parameter summary table. State units and that fits are printed
  clearances, not toleranced manufacturing specs.

## Change discipline

- **Judge the cumulative part, not the single edit.** After any change, re-render
  and re-validate; a change "elsewhere" can still break a joint, a cap, or
  manifoldness.
- **Surface tradeoffs honestly.** If a requested goal conflicts with a validated
  feature (e.g. "make it narrower" vs. a side joint that sets the minimum width),
  explain the floor and offer an alternative layout rather than silently
  weakening the part.
- **One change at a time when fit is involved**, so a regression is traceable to
  its cause.
