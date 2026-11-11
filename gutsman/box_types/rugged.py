#!/usr/bin/env python3
"""Rugged tool box — base + lid on a screwed hinge, with clasps.

Owns its whole wizard: the questions, the advanced table, the edit table, the
build and the renders. Another box type is a sibling module with its own flow,
not a set of parameters passed into this one.

Geometry comes from the vendored generator in box-builder/ (engine is
smkent/monoscad rugged-box, CC BY-SA 4.0). We drive its documented bot
interface — `box_wizard.py --json` — rather than importing its Python API, so a
renderer hang or crash stays in its own process behind a timeout.
"""
import asyncio
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import boxmeta
import cubby
import render
import spec
from wizard_base import (BaseWizard, allowed_text, fmt, parse_answer,
                         sanitize_name, table)

COMMAND     = "rugged"
TITLE       = "Rugged Box"
DESCRIPTION = "Design a 3D-printable rugged tool box (wizard)"

ROOT    = Path(__file__).resolve().parent.parent / "box-builder"
CLI     = ROOT / "scripts" / "box_wizard.py"
TIMEOUT = 420               # a `draw` clasp adds ~20s; 5 min ceiling + slack

# The generator takes 0 to mean "auto" and resolves it to 1 or 2 — it cannot
# build a clasp-less box. A literal "0" in the summary reads as "no clasps", so
# the wizard says `auto` and translates it on the way out.
LATCH_AUTO = "auto"

# Tray-to-case gap per side: snug but still removable (FDM, 0.4 nozzle).
CASE_CLEARANCE = 0.15
# Roughly what fits the generator's label plate before it overruns.
MAX_LABEL_CHARS = 20
# Lid depth defaulted when a cubby is chosen — snug, so a closed lid keeps parts
# from rattling in their compartments (vs the roomy 20 mm plain-box default).
SNUG_LID_DEPTH = 5

# Asked, in order. Only what the box genuinely can't be built without — and in
# `cubby` mode even these are derived from the compartments, so the flow is
# name → compartments → depth → confirm.
STEPS = [
    dict(key="Width", kind="int", lo=20, hi=300, default=180,
         prompt="Interior **width** — side-to-side, mm", hint="20–300"),
    dict(key="Length", kind="int", lo=20, hi=300, default=90,
         prompt="Interior **length** — front-to-back, mm", hint="20–300"),
    dict(key="Bottom_Height", kind="int", lo=0, hi=300, default=40,
         prompt="Interior **base** depth, mm", hint="0–300"),
]

# NOT asked. Every one of these has a default that builds a good box, so making
# them questions cost a Discord round trip each to answer "yes, the usual" —
# eight of them before the user could see a summary. They are listed in the
# confirm summary and changed with `edit`, which keeps them discoverable without
# putting them in everyone's way. This is the "easy by default, customisable on
# demand" split; the same idea as ADVANCED, one level up.
OPTIONS = [
    dict(key="Top_Height", kind="int", lo=0, hi=300, default=20,
         label="Lid depth (mm)"),
    dict(key="Latch_Count", kind="choice", choices=[LATCH_AUTO, "1", "2"],
         default="2", label="Clasps"),
    dict(key="Latch_Type", kind="choice", choices=["clip", "draw"],
         default="clip", label="Clasp style"),
    dict(key="Handle", kind="bool", default=False, label="Carry handle"),
    dict(key="Label", kind="bool", default=False, label="Label plate"),
    dict(key="Label_Text", kind="str", default="Label", only_if="Label",
         label="Label text"),
]

ADVANCED = [
    dict(key="Lip_Seal_Type", kind="choice", default="wedge",
         choices=["none", "wedge", "square", "filament-1.75mm"], label="Lip seal"),
    dict(key="Corner_Radius", kind="float", lo=0, hi=20, default=5.0,
         label="Corner radius (mm)"),
    dict(key="Reinforced_Corners", kind="bool", default=False,
         label="Reinforced corners"),
    dict(key="Top_Grip", kind="bool", default=False,
         label="Lid front grip (2-clasp only)"),
    dict(key="Wall_Thickness", kind="float", lo=0.4, hi=10, default=3.0,
         label="Wall thickness (mm)"),
    dict(key="Lip_Thickness", kind="float", lo=0.4, hi=10, default=3.0,
         label="Lip thickness (mm)"),
]

DEFAULTS = {o["key"]: o["default"] for o in STEPS + OPTIONS + ADVANCED}
PART_ORDER = ("bottom", "top", "latch", "handle", "label", "insert")


# ── generation ────────────────────────────────────────────────────────────
def _cli_args(params: dict) -> list:
    args = []
    for key, val in params.items():
        flag = "--" + key.lower().replace("_", "-")
        if isinstance(val, bool):
            args.append(flag if val else "--no-" + key.lower().replace("_", "-"))
        else:
            args += [flag, str(val)]
    return args


def normalize(params: dict) -> dict:
    """Wizard answers -> what the generator expects."""
    p = dict(params)
    if not p.get("Label"):
        p.pop("Label_Text", None)       # engine ignores it; keeps the CLI tidy
    if p.get("Latch_Count") == LATCH_AUTO:
        p["Latch_Count"] = 0            # the engine's spelling of "auto"
    return p


def check_only(params: dict, timeout: int = 60) -> list:
    """Ask the generator whether these options are buildable, without rendering.

    Same documented `--json` CLI as `build()`, with `--check-only`: it produces
    no geometry and returns in well under a second, so the wizard can say "that
    handle won't fit" *at the moment the user asks for it* instead of after a
    minute of rendering. Re-deriving the engine's handle/label gates in Python
    would silently drift from the SCAD, which is exactly why box-builder exposes
    this.

    Returns a list of error strings — empty if the options are fine, and also
    empty if the generator can't be reached, since the real build validates
    again anyway and a probe failure shouldn't block the wizard.
    """
    if not CLI.exists():
        return []
    cmd = [sys.executable, str(CLI), "--json", "--check-only"] + _cli_args(params)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(ROOT))
        return json.loads(r.stdout).get("errors") or []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError,
            AttributeError):
        return []


def build(params: dict, out_dir: Path) -> dict:
    """Run the generator. Returns its JSON result, or a synthetic error dict."""
    if not CLI.exists():
        return {"status": "unavailable",
                "errors": [f"generator missing — expected `{CLI}`. Re-extract "
                           "box-builder into the BoxBot folder."]}
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(CLI), "--json", "--out", str(out_dir)] \
        + _cli_args(params)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"status": "timeout",
                "errors": [f"timed out after {TIMEOUT // 60} min — try a "
                           "smaller box or the `clip` clasp."]}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        tail = ((r.stderr or r.stdout or "").strip().splitlines()
                or ["(no output)"])[-6:]
        return {"status": "render_failed", "errors": tail}


# ── renders ───────────────────────────────────────────────────────────────
def _hinge_axis(mesh, samples: int = 14):
    """Locate the hinge screw-bore axis: the round void running along X on the
    +Y (hinge) side of a part. Returns the axis z, or None.

    Both halves carry their hinge at the same +Y, so aligning these two z's is
    what seats the lid on the base. Stacking by bounding box instead leaves the
    lid floating ~9mm high, which reads as a broken hinge in the render.
    """
    import numpy as np
    from shapely.geometry import Polygon

    x0, x1 = mesh.bounds[0][0], mesh.bounds[1][0]
    y_max = mesh.bounds[1][1]
    hits = []
    for x in np.linspace(x0 + 2, x1 - 2, samples):
        try:
            sec = mesh.section(plane_origin=[x, 0, 0], plane_normal=[1, 0, 0])
            if sec is None:
                continue
            planar, to_3d = sec.to_planar()
        except Exception:  # noqa: BLE001 — degenerate slice, just skip it
            continue
        for poly in planar.polygons_full:
            for ring in poly.interiors:          # bores are interior rings
                g = Polygon(ring)
                if g.area <= 0 or g.length <= 0:
                    continue
                r = math.sqrt(g.area / math.pi)
                if not (0.8 <= r <= 3.0):        # M3 clearance ≈ 1.45–1.8mm
                    continue
                if 4 * math.pi * g.area / g.length ** 2 < 0.85:
                    continue                     # not round — a slot or rib gap
                c = g.centroid
                w = to_3d @ np.array([c.x, c.y, 0.0, 1.0])
                if w[1] > y_max * 0.7:           # hinge side, not the clasp side
                    hits.append(w[2])
    return float(np.median(hits)) if hits else None


def render_shots(files: dict, out_dir: Path):
    """Two images: the assembled box showing both faces, and the parts flat."""
    if not render.have_openscad():
        return [], ["`openscad` not installed — skipped the renders."]
    meshes, bounds, warns = render.load_bounds(files)
    if "bottom" not in bounds or "top" not in bounds:
        return [], warns + ["missing base/lid STL — skipped the renders."]

    # Seat the lid by matching hinge axes; fall back to a rim-to-rim stack.
    hz_base, hz_lid = _hinge_axis(meshes["bottom"]), _hinge_axis(meshes["top"])
    if hz_base is not None and hz_lid is not None:
        dz = hz_base + hz_lid           # the lid is flipped, so its z negates
    else:
        dz = ((bounds["bottom"][1][2] - bounds["bottom"][0][2])
              + (bounds["top"][1][2] - bounds["top"][0][2]))
        warns.append("couldn't find the hinge axis — lid placed by bounding box.")

    assembled = (render.stl_import(files["bottom"], "bottom")
                 + f'\ntranslate([0,0,{dz:.3f}]) rotate([0,180,0]) '
                 + render.stl_import(files["top"], "top"))

    # Two copies, the right one yawed 180°, so one image shows the hinge face
    # and the clasp face together.
    width = bounds["bottom"][1][0] - bounds["bottom"][0][0]
    depth = bounds["bottom"][1][1] - bounds["bottom"][0][1]
    gap = max(12.0, width * 0.12)
    both = (f"translate([{-(width + gap) / 2:.2f},0,0]) {{{assembled}}}\n"
            f"translate([{(width + gap) / 2:.2f},0,0]) rotate([0,0,180]) "
            f"{{{assembled}}}")
    asm_h = dz + (bounds["top"][1][2] - bounds["top"][0][2])

    parts_src, pw, pd, ph = render.parts_grid(files, bounds, PART_ORDER)

    shots = []
    for name, src, cam, size in (
            ("assembled", both, "0,0,0,62,0,25,0",
             render.canvas(2 * width + gap, depth, asm_h, 62)),
            ("parts", parts_src, "0,0,0,58,0,18,0",
             render.canvas(pw, pd, ph, 58))):
        png = out_dir / f"render-{name}.png"
        ok, err = render.shoot(src, png, cam, size)
        if ok:
            shots.append(png)
        else:
            warns.append(f"{name} render failed: {err}")
    return shots, warns


# ── the wizard ────────────────────────────────────────────────────────────
class Wizard(BaseWizard):
    """name → what's inside → the rest → confirm, with `edit`/`advance` detours.

    The **inside** comes first and drives the build: list the compartments you
    need (`60x40, 60x20`) and the box interior, walls and a matching tray are
    sized around them — no wall/divider math for the user. Reply `open` instead
    to give the interior yourself (with an optional even split of cubbies).
    """

    COMMAND = COMMAND
    TITLE = TITLE

    def __init__(self, cfg, logger):
        super().__init__(cfg, logger)
        self.idx = 0
        self.params = dict(DEFAULTS)
        # Inside-first state: 'sizes' derives the interior from listed cells;
        # 'open' asks for it. `prefilled` are STEP keys the inside flow answered.
        self.mode = None
        self.cell_vectors = None     # (col_widths, row_lengths) if a grid
        self.layout_pockets = None   # (pockets, span_w, span_l) — the build form
        self.layout_note = ""        # human description of the arrangement
        self.layout = None           # spec.LayoutBuilder while designing the grid
        self.compartment_depth = None
        self.prefilled = set()

    def reset(self):
        super().reset()
        self.idx = 0
        self.params = dict(DEFAULTS)
        self.mode = None
        self.cell_vectors = None
        self.layout_pockets = None
        self.layout_note = ""
        self.layout = None
        self.compartment_depth = None
        self.prefilled = set()

    def start(self) -> str:
        self.reset()
        self.state = "name"
        self.log.info("rugged wizard started")
        return (
            f"📦 **{TITLE} Builder**\n\n"
            "First I'll ask which **insert** you want; that sizes the box. Then a "
            "few questions about the shell, and I generate and printability-test "
            "the parts. Reply `-` to accept a default, `cancel` to stop.\n\n"
            "**Name** — what should I call this box?\n"
            "*(lowercase, hyphens OK — becomes the output folder)*"
        )

    # ── questions ─────────────────────────────────────────────────────────
    def _steps(self) -> list:
        """Live questions: drop any already answered by the inside flow (the
        interior dims, in `sizes` mode)."""
        return [s for s in STEPS if s["key"] not in self.prefilled]

    def _options(self) -> list:
        """Editable-but-unasked settings, minus any gated off (Label_Text)."""
        return [o for o in OPTIONS
                if not o.get("only_if") or self.params.get(o["only_if"])]

    def _insert_prompt(self, note: str = "") -> str:
        return ((note + "\n\n" if note else "")
                + "**Which insert do you want?** This decides how I size the box.\n"
                "• `cubby` — a divided tray. I'll ask the compartment sizes and "
                "build the box around them.\n"
                "• `none` — no insert; I'll ask for the interior dimensions "
                "instead.")

    def _layout_step(self, text: str):
        """Delegate the grid design to the shared LayoutBuilder, rendering a
        preview image whenever it asks for one, until the layout is confirmed."""
        res = self.layout.feed(text)
        if res.done:
            b = self.layout
            self.cell_vectors = None if b.ragged else (b.col_widths, b.row_lengths)
            self.layout_pockets = (b.pockets, b.span_w, b.span_l)
            self.layout_note = b.note
            self.state = "insert_depth"
            return "Layout locked in.\n\n**How deep** should the compartments be? (mm)"
        if res.preview:
            png = Path(tempfile.gettempdir()) / f"boxbot-layout-{id(self)}.png"
            img = render.layout_png(self.layout.pockets, self.layout.span_w,
                                    self.layout.span_l, png,
                                    title=self.layout.image_title())
            if img:
                return res.reply, [img]
        return res.reply

    def _next_question(self) -> str:
        steps = self._steps()
        if self.idx >= len(steps):
            self.state = "confirm"
            return self._summary()
        opt = steps[self.idx]
        hint = f"\n*({opt['hint']})*" if opt.get("hint") else ""
        return (f"**Step {self.idx + 1} of {len(steps)}** — {opt['prompt']}{hint}\n"
                f"Default: `{fmt(self.params[opt['key']])}` — reply `-` to keep it.")

    # ── insert-first: the insert (and its sizes) drive the box ────────────
    def _insert_step(self, text: str):
        low = text.lower()
        if low in ("cubby", "tray", "insert", "cubbies", "divided", "1", "yes"):
            self.layout = spec.LayoutBuilder()
            self.state = "layout"
            return self.layout.prompt()
        if low in ("none", "no", "open", "empty", "plain", "skip", "2"):
            self.mode = "open"
            self.state = "steps"
            self.idx = 0
            return ("No insert — I'll ask for the interior.\n\n"
                    + self._next_question())
        return self._insert_prompt("❌ Reply `cubby` for a divided tray, or "
                                   "`none` for a plain box.")

    def _insert_depth_step(self, text: str) -> str:
        try:
            depth = float(text)
        except ValueError:
            return "Enter the compartment depth in mm, e.g. `40`."
        if not 5 <= depth <= 300:
            return "Depth must be 5–300 mm. Try again."
        self.compartment_depth = depth
        _, span_w, span_l = self.layout_pockets
        self.mode = "sizes"
        interior = spec.interior_from_span(span_w, span_l, depth,
                                           clearance=CASE_CLEARANCE)
        self.params["Width"] = interior["width"]
        self.params["Length"] = interior["length"]
        self.params["Bottom_Height"] = interior["height"]
        # A snug lid by default so it keeps parts seated in their cubbies; still
        # asked, so the user can open it up for taller contents.
        self.params["Top_Height"] = SNUG_LID_DEPTH
        # These are derived now, so the linear flow shouldn't ask for them.
        self.prefilled = {"Width", "Length", "Bottom_Height"}
        self.state = "steps"
        self.idx = 0
        note = (f"Sized the interior to **{interior['width']} × "
                f"{interior['length']} × {interior['height']} mm** around your "
                f"compartments.\n\n")
        return note + self._next_question()

    async def step(self, message: str):
        text = message.strip()
        if text.lower() == "cancel":
            return self.cancel()

        if self.state == "name":
            name = sanitize_name(text)
            if not name:
                return "That name didn't work — letters, numbers, hyphens or underscores."
            if self.name_taken(name):
                return f"A box called `{name}` already exists. Pick another name."
            self.name = name
            self.state = "insert"
            return f"Got it — **{name}**.\n\n" + self._insert_prompt()

        if self.state == "insert":
            return self._insert_step(text)

        if self.state == "layout":
            return self._layout_step(text)

        if self.state == "insert_depth":
            return self._insert_depth_step(text)

        if self.state == "steps":
            opt = self._steps()[self.idx]
            if text != "-":
                val, err = parse_answer(opt, text)
                if err:
                    return err
                self.params[opt["key"]] = val
            self.idx += 1
            return self._next_question()

        if self.state == "advance":
            return self._advance_step(text)

        if self.state == "edit":
            return self._edit_step(text)

        if self.state == "confirm":
            low = text.lower()
            if low in ("go", "yes", "y", "build", "ok", "yep", "yeah", "do it"):
                return await self._execute()
            if low in ("advance", "advanced", "adv", "more"):
                self.state = "advance"
                return self._advanced_table()
            if low in ("edit", "back", "change", "revise"):
                self.state = "edit"
                return self._edit_table()
            if low in ("no", "n", "cancel", "abort"):
                return self.cancel()
            shortcut = await self._confirm_shortcut(text)
            if shortcut is not None:
                return shortcut
            return ("Reply **go** to build, **edit** to change a setting, "
                    "**advance** for more options, or **cancel**.")

        return "Something went wrong — type `cancel` to reset."

    # ── advanced options ──────────────────────────────────────────────────
    def _advanced_table(self, note: str = "") -> str:
        rows = [f"{i:>2}  {o['label']:<22} {fmt(self.params[o['key']]):<12} "
                f"{allowed_text(o)}" for i, o in enumerate(ADVANCED, 1)]
        return table("⚙️ **Advanced options**", ("option", "current", "allowed"),
                     rows,
                     "Reply `<number> <value>` to change one *(e.g. `1 square`)*.\n"
                     "Reply `done` when finished, or `cancel` to abort.", note)

    def _advance_step(self, text: str) -> str:
        if text.lower() in ("done", "back", "go", "ok"):
            self.state = "confirm"
            return self._summary()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return self._advanced_table("❌ Use `<number> <value>`, e.g. `1 square`.")
        n = int(parts[0])
        if not 1 <= n <= len(ADVANCED):
            return self._advanced_table(f"❌ Pick a number 1–{len(ADVANCED)}.")
        opt = ADVANCED[n - 1]
        val, err = parse_answer(opt, parts[1])
        if err:
            return self._advanced_table(f"❌ {opt['label']}: {err}")
        self.params[opt["key"]] = val
        return self._advanced_table(f"✅ {opt['label']} → `{fmt(val)}`")

    # ── going back to change an answer ────────────────────────────────────
    def _edit_rows(self) -> list:
        # Name, the asked questions, then everything that was defaulted — the
        # unasked OPTIONS are only reachable here, so they must be listed.
        return [None] + self._steps() + self._options()

    @staticmethod
    def _row_label(o: dict) -> str:
        return o.get("label") or o["key"].replace("_", " ")

    def _edit_table(self, note: str = "") -> str:
        rows = []
        for i, o in enumerate(self._edit_rows(), 1):
            if o is None:
                rows.append(f"{i:>2}  {'Name':<22} {self.name:<12} text")
            else:
                rows.append(f"{i:>2}  {self._row_label(o):<22} "
                            f"{fmt(self.params[o['key']]):<12} {allowed_text(o)}")
        return table("✏️ **Your answers**", ("setting", "current", "allowed"),
                     rows,
                     "Reply `<number> <value>` to change one *(e.g. `2 200`)*.\n"
                     "Reply `done` to go back to the summary, or `cancel` to abort.",
                     note)

    def _edit_step(self, text: str) -> str:
        if text.lower() in ("done", "back", "go", "ok"):
            self.state = "confirm"
            return self._summary()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return self._edit_table("❌ Use `<number> <value>`, e.g. `2 200`.")
        rows = self._edit_rows()
        n = int(parts[0])
        if not 1 <= n <= len(rows):
            return self._edit_table(f"❌ Pick a number 1–{len(rows)}.")
        opt = rows[n - 1]

        if opt is None:                             # renaming the box
            name = sanitize_name(parts[1])
            if not name:
                return self._edit_table("❌ Name: letters, numbers, hyphens or "
                                        "underscores.")
            if name != self.name and self.name_taken(name):
                return self._edit_table(f"❌ A box called `{name}` already exists.")
            self.name = name
            return self._edit_table(f"✅ Name → `{name}`")

        val, err = parse_answer(opt, parts[1])
        if err:
            return self._edit_table(f"❌ {self._row_label(opt)}: {err}")
        self.params[opt["key"]] = val
        note = f"✅ {self._row_label(opt)} → `{fmt(val)}`"
        if opt["key"] == "Label" and val:
            note += " — set its text with the new row below."
        return self._edit_table(note)

    # ── confirm + build ───────────────────────────────────────────────────
    def _summary(self) -> str:
        """Everything that was decided for the user, on one screen.

        Since the clasp/handle/label questions are no longer asked, this is
        where they surface — a user who wants something different sees it here
        and replies `edit`, instead of being interrogated about it up front.
        """
        p = self.params
        clasps = ("auto" if p["Latch_Count"] == LATCH_AUTO else p["Latch_Count"])
        changed = [f"{o['label']} = {fmt(p[o['key']])}" for o in ADVANCED
                   if p[o["key"]] != o["default"]]
        extra = ("\n**Advanced:** " + "; ".join(changed)) if changed else ""
        # The two features people most often want. Each gets its own line with
        # the exact words that turn it on, because "somewhere under edit" is not
        # discoverable — you have to be told, on the screen where you decide.
        handle = (f"🖐️ **Carry handle:** on — reply `handle` to remove"
                  if p["Handle"] else
                  "🖐️ **Carry handle:** off — reply `handle` to add one")
        label = (f"🏷️ **Label plate:** “{p['Label_Text']}” — reply `label off` "
                 "to remove" if p["Label"] else
                 "🏷️ **Label plate:** off — reply `label TOOLS` to add one")
        return (
            f"📦 **{self.name}** — ready to build\n\n"
            + self._inside_summary()
            + f"**Interior:** {p['Width']} × {p['Length']} mm · "
            f"base {p['Bottom_Height']} mm + lid {p['Top_Height']} mm\n"
            f"**Clasps:** {clasps} ({p['Latch_Type']})\n"
            f"{handle}\n{label}"
            + extra + "\n"
            f"📁 `{self.boxes_root() / self.name}`\n\n"
            "**go** to build · **edit** to change any setting above · "
            "**advance** for walls, seal & corners · **cancel**"
        )

    # ── one-word shortcuts from the summary ───────────────────────────────
    def _shortcut_reply(self, what: str, value: str, warn: str = "") -> str:
        return (f"✅ {what} → **{value}**" + (f"\n⚠️ {warn}" if warn else "")
                + "\n\n" + self._summary())

    async def _try_feature(self, changes: dict):
        """Apply changes only if the generator says the box can still be built.

        The handle and label have real geometric requirements (a 2-latch box,
        enough height and front face). Since the summary now actively invites
        the user to switch them on, letting an impossible one through would mean
        advertising a feature and then failing the build a minute later. Asking
        the engine costs ~0.2 s, so the answer arrives with the reply.

        Returns an error string, or "" if the change stuck.
        """
        before = {k: self.params[k] for k in changes}
        self.params.update(changes)
        errs = await asyncio.to_thread(check_only, normalize(dict(self.params)))
        if errs:
            self.params.update(before)          # nothing half-applied
            return " ".join(errs)
        return ""

    async def _confirm_shortcut(self, text: str):
        """Make the summary's suggestions literally typeable.

        `handle` and `label TOOLS` are the settings users reach for most, and
        without this they cost `edit` → find the row number → `done`. Returns
        None if the text isn't a shortcut, so the normal confirm help still
        shows for anything else.
        """
        parts = text.split(None, 1)
        word = parts[0].lower()
        arg = (parts[1].strip() if len(parts) > 1 else "")
        off = ("off", "no", "none", "remove", "n")
        on = ("on", "yes", "add", "y")

        if word in ("handle", "carry"):
            if arg.lower() in off:
                want = False
            elif arg.lower() in on or not arg:
                # bare `handle` toggles, so it both adds and removes
                want = True if arg else not self.params["Handle"]
            else:
                return None
            if not want:
                self.params["Handle"] = False
                return self._shortcut_reply("Carry handle", "off")
            err = await self._try_feature({"Handle": True})
            if err:
                return f"❌ {err}\n\n" + self._summary()
            return self._shortcut_reply("Carry handle", "on")

        if word in ("label", "plate", "engrave", "text"):
            if arg.lower() in off:
                self.params["Label"] = False
                return self._shortcut_reply("Label plate", "off")
            if not arg:
                return ("What should the label say? Reply `label TOOLS` "
                        "(or `label off` to remove it).")
            if len(arg) > MAX_LABEL_CHARS:
                return (f"That's {len(arg)} characters — keep the label to "
                        f"{MAX_LABEL_CHARS} or fewer.\n\n" + self._summary())
            err = await self._try_feature({"Label": True, "Label_Text": arg})
            if err:
                return f"❌ {err}\n\n" + self._summary()
            return self._shortcut_reply("Label plate", f"“{arg}”")
        return None

    def _inside_summary(self) -> str:
        """The 'insert' line, shown above the derived interior."""
        if self.mode == "sizes" and self.layout_pockets:
            return (f"**Insert:** cubby — {self.layout_note}, "
                    f"{self.compartment_depth:g} mm deep\n")
        return "**Insert:** none (open box)\n"

    async def _execute(self):
        name    = self.name
        out_dir = self.boxes_root() / name
        raw     = dict(self.params)          # keep the wizard-only answers
        inside  = (self.mode, self.layout_pockets, self.compartment_depth)
        params  = normalize(self.params)
        self.reset()

        self.log.info(f"rugged build starting: {name} {params}")
        result = await asyncio.to_thread(build, params, out_dir)
        return await asyncio.to_thread(self._finish, name, out_dir, params,
                                       result, raw, inside)

    def _add_cubby(self, raw, inside, files, out_dir, warns):
        """Build the divided tray from the listed compartments, if any.

        Only the `cubby` insert choice produces a tray, and it builds the exact
        cells the user listed (via `cubby.build`). `open` boxes get no tray here
        — they can still take one later through `/inserts`. Appends `insert` to
        `files` so it lands in the parts render; a tray that can't build is
        skipped with a warning and the box still ships.
        """
        mode, layout, depth = inside
        if mode != "sizes" or not layout:
            return None
        pockets, span_w, span_l = layout
        # Scale the layout to the (rounded) box interior so the tray sits in the
        # case at exactly CASE_CLEARANCE per side — snug but removable.
        fitted, fw, fl = cubby.fit_pockets(raw["Width"], raw["Length"], pockets,
                                           span_w, span_l, CASE_CLEARANCE)
        out = out_dir / "insert.stl"
        ok, err = cubby.build_pockets(fitted, fw, fl, spec.tray_height(depth), out,
                                      corner_radius=max(0.0, raw["Corner_Radius"] - CASE_CLEARANCE))
        if not ok:
            warns.append(f"insert tray failed to build: {err}")
            return None
        files["insert"] = str(out)
        n = len(pockets)
        return f"custom tray, {n} compartment{'s' if n != 1 else ''}"

    def _finish(self, name, out_dir, params, result, raw, inside):
        status = result.get("status")
        if status in ("invalid", "unavailable", "timeout"):
            errs = "\n".join(f"  • {e}" for e in result.get("errors", []))
            return (f"❌ **{name}** can't be built:\n{errs}\n\n"
                    f"Nothing was rendered. Run `/{COMMAND}` again.")
        if status == "render_failed":
            errs = "\n".join(f"  • {e}" for e in result.get("errors", [])[:5])
            return f"❌ **{name}** failed to render:\n```\n{errs}\n```"

        files = result.get("files", {})
        warns = []
        # Build the tray (if any) before rendering so it lands in the parts grid.
        cubby_note = self._add_cubby(raw, inside, files, out_dir, warns)
        shots, rwarns = render_shots(files, out_dir)
        warns += rwarns

        # Record what this box is, so /inserts can build for it later.
        boxmeta.write(out_dir, boxmeta.make(
            name, "rugged",
            {"width": raw["Width"], "length": raw["Length"],
             "height": raw["Bottom_Height"]},
            insert_supported=True, corner_radius=raw["Corner_Radius"]))
        self.log.info(f"rugged build finished: {name} status={status} "
                      f"shots={len(shots)} cubby={cubby_note}")

        # The engine reports what "auto" resolved to, so this is the real count.
        n_latch = int(result.get("latch_count") or params.get("Latch_Count") or 0)
        screws = 1 * 2 + 2 * n_latch        # 1 per hinge (x2) + 2 per clasp
        lines = [
            (f"✅ **{name}** — built and verified" if status == "ok"
             else f"⚠️ **{name}** — built, but **printability tests FAILED**"),
            "",
            f"**Interior:** {params['Width']} × {params['Length']} mm, "
            f"base {params['Bottom_Height']} mm + lid {params['Top_Height']} mm",
            "**Parts:** " + ", ".join(f"{n_latch}× latch" if p == "latch"
                                      else f"1× {p}" for p in files),
            f"**Hardware:** ~{screws} × M3×40 screws",
            "**Print:** 0.2 mm layers, no supports (Prusa Core One, 0.4 mm nozzle)",
            f"**Render:** {result.get('render_seconds', '?')}s",
            "",
            f"📁 `{out_dir}`",
        ]
        if cubby_note:
            lines.insert(4, f"**Insert:** {cubby_note}")
        for w in result.get("warnings", []) + warns:
            lines.append(f"⚠️ {w}")
        if status != "ok":
            tail = (result.get("test_output") or "").strip().splitlines()[-8:]
            if tail:
                lines += ["", "```", *tail, "```"]
            lines.append("\n**Don't print this** until the failure is understood.")
        return "\n".join(lines), shots
