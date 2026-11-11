#!/usr/bin/env python3
"""Circular box — a round container with a press-fit lip lid or a screw lid.

Owns its whole wizard, like every box type. This is ROADMAP Phase 3, and the
first box type with no vendored geometry ancestor: `scad/circular_box.scad` was
written for BoxBot, so nothing here carries a third-party licence.

Interior-first like the rest, but the interior of a cylinder is a *diameter*,
not a width and a length — that one substitution is most of what makes this
module differ from `project.py`. Two consequences worth knowing:

* **The lip lid eats depth.** Its ring descends into the bore, so the clear
  depth under a closed lid is `body_height - floor - lip_height`. `_defines`
  adds the lip back when deriving the body so the user still gets the depth
  they asked for. A screw lid sits flat on top and costs nothing (same as
  project.py's flat lid).
* **A cubby tray is rectangular; the box is not.** A tray fits when its
  *diagonal* clears the bore, so a compartment layout sizes the box by
  `hypot(width, length)`, not by its sides. The tray is built at its natural
  span rather than stretched to fill the interior — there is no rectangle to
  fill, and scaling it up would push its corners through the wall.
"""
import asyncio
import math
from pathlib import Path

import boxmeta
import cubby
import render
import spec
from wizard_base import (BaseWizard, allowed_text, fmt, parse_answer,
                         sanitize_name, table)

COMMAND     = "circular"
TITLE       = "Circular Box"
DESCRIPTION = "Design a 3D-printable round box with a press-fit or screw lid (wizard)"

SCAD = Path(__file__).resolve().parent.parent / "scad" / "circular_box.scad"

# Fixed engineering values the wizard doesn't ask about — an M3 self-tapping
# boss sized for PLA, matching project.py's ladder so the two boxes take the
# same hardware.
HOLE_DIA_LID    = 3.4    # M3 clearance through the lid
HOLE_DIA_THREAD = 2.5    # M3 self-tap pilot into the ear
SCREW_POST_DIA  = 8.0
CS_HEAD_DIA     = 5.5
NS_HEAD_DIA     = 6.0
NS_HEAD_HIGH    = 2.5
LIP_WALL        = 1.6    # thickness of the lid's lip ring
MIN_BOTTOM      = 2.0    # mm of ear left below the pilot hole, minimum
HOLE_DEEPNESS_DEFAULT = 10.0

# How far an ear stands proud of the wall, as a fraction of its diameter. Must
# match EAR_MERGE in the SCAD: the ear centre is pulled back into the wall, so
# its outer edge reaches `0.65 * screw_post_dia` past the outer radius. Only
# used to predict the printed footprint for the bed check.
EAR_PROUD = 0.65

# Tray-to-bore gap per side when a cubby insert is chosen — matches rugged's.
CASE_CLEARANCE = 0.15

BED_MM = 250             # Prusa Core One; same constant as sliding.py

SCREW_STYLES = {"flush": 1, "raised": 2, "hole-only": 0}
LID_STYLES = ("lip", "screw")

STEPS = [
    dict(key="Internal_Diameter", kind="int", lo=20, hi=220, default=80,
         prompt="Interior **diameter** — across the inside, mm", hint="20–220"),
    dict(key="Internal_Height", kind="int", lo=10, hi=200, default=40,
         prompt="Interior **depth** (Z), mm", hint="10–200"),
    dict(key="Lid_Style", kind="choice", default="lip", choices=list(LID_STYLES),
         prompt="**Lid style** — `lip` (press-fit, no hardware) or `screw` "
                "(screwed down through ears)"),
]

ADVANCED = [
    dict(key="Wall_Thickness", kind="float", lo=1.2, hi=8, default=2.4,
         label="Wall thickness (mm)"),
    dict(key="Floor_Thickness", kind="float", lo=1.0, hi=8, default=2.0,
         label="Floor thickness (mm)"),
    dict(key="Lid_Thickness", kind="float", lo=1.2, hi=8, default=2.4,
         label="Lid thickness (mm)"),
    dict(key="Tolerance", kind="float", lo=0.05, hi=1.0, default=0.25,
         label="Lip fit tolerance (mm)"),
    dict(key="Lip_Height", kind="float", lo=3, hi=20, default=6.0,
         label="Lip depth (mm)"),
    dict(key="Screw_Count", kind="int", lo=2, hi=8, default=3,
         label="Screw ears"),
    dict(key="Screw_Style", kind="choice", default="flush",
         choices=list(SCREW_STYLES), label="Screw head style"),
]

DEFAULTS = {o["key"]: o["default"] for o in STEPS + ADVANCED}
PART_ORDER = ("body", "lid")


# ── interior -> outer + SCAD defines ───────────────────────────────────────
def _defines(p: dict) -> dict:
    """Turn interior answers into the SCAD's outer-dimension parameters.

    Inverts the SCAD's own derivation so the user gets exactly the cavity they
    asked for: the bore is `outer_dia - 2*wall`, and the clear depth under a
    closed lid is `body_height - floor - lip` (the lip ring hangs into the
    bore; a screw lid contributes nothing).
    """
    wall = p["Wall_Thickness"]
    floor = p["Floor_Thickness"]
    lip = p["Lip_Height"] if p["Lid_Style"] == "lip" else 0.0
    body_height = p["Internal_Height"] + floor + lip
    hole_deepness = min(HOLE_DEEPNESS_DEFAULT, max(4.0, body_height - MIN_BOTTOM))
    return {
        "outer_dia": round(p["Internal_Diameter"] + 2 * wall, 3),
        "body_height": round(body_height, 3),
        "wall_thickness": wall,
        "floor_thickness": floor,
        "lid_thickness": p["Lid_Thickness"],
        "tolerance": p["Tolerance"],
        "lid_style": p["Lid_Style"],
        "lip_height": p["Lip_Height"],
        "lip_wall": LIP_WALL,
        "screw_count": p["Screw_Count"],
        "screw_post_dia": SCREW_POST_DIA,
        "hole_dia_lid": HOLE_DIA_LID,
        "hole_dia_thread": HOLE_DIA_THREAD,
        "hole_deepness": round(hole_deepness, 3),
        "hole_type": SCREW_STYLES[p["Screw_Style"]],
        "cs_head_dia": CS_HEAD_DIA,
        "ns_head_dia": NS_HEAD_DIA,
        "ns_head_high": NS_HEAD_HIGH,
    }


def footprint(defs: dict) -> float:
    """Widest span the printed part occupies — the bed check's input.

    A lip box is just its outer diameter; screw ears stand proud of the wall, so
    the envelope grows by an ear on each side.
    """
    if defs["lid_style"] != "screw":
        return defs["outer_dia"]
    return defs["outer_dia"] + 2 * EAR_PROUD * defs["screw_post_dia"]


def diameter_for_span(interior: dict) -> int:
    """Bore diameter that clears a rectangular tray of this interior footprint.

    A rectangle fits a circle when its *diagonal* does, so the box is sized by
    the hypotenuse rather than by either side. Rounded up to whole mm, matching
    the integer interiors the other wizards work in.
    """
    return math.ceil(math.hypot(interior["width"], interior["length"]))


def _meta(name: str, params: dict) -> dict:
    """The `box.json` payload for a finished round box.

    Round boxes are **not** `/inserts`-ready, and that is deliberate: that
    command sizes a rectangular tray from `internal` width/length, which has no
    honest answer for a cylinder. Reporting the diameter would produce a tray
    whose corners foul the wall; reporting the inscribed square that *would* fit
    would misreport the box's actual size to everything else that reads this
    file. So `internal` records the true interior — width = length = diameter —
    and `insert.supported` is False, which keeps the box out of `/inserts`'
    picker entirely (`boxmeta.scan` filters on exactly that flag).

    A divided tray is still available *inside* `/circular` itself, where the
    wizard knows it is building for a bore and sizes it by the diagonal.
    """
    dia = params["Internal_Diameter"]
    return boxmeta.make(
        name, "circular",
        {"width": dia, "length": dia, "height": params["Internal_Height"]},
        insert_supported=False,
        corner_radius=0.0)


def build(defines: dict, out_dir: Path) -> dict:
    """Render the body and lid STLs. Returns a result dict shaped like sliding's."""
    if not render.have_openscad():
        return {"status": "unavailable",
                "errors": ["`openscad` not installed — can't build geometry."]}
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for part, fname in (("box", "body.stl"), ("lid", "lid.stl")):
        out = out_dir / fname
        ok, err = render.build_stl(SCAD, out, dict(defines, render_part=part))
        if not ok:
            return {"status": "render_failed", "errors": [f"{part}: {err}"]}
        files["body" if part == "box" else "lid"] = str(out)
    return {"status": "ok", "files": files}


def render_shots(defines: dict, out_dir: Path):
    """Assembled + parts previews, straight from the SCAD's own layout."""
    if not render.have_openscad():
        return [], ["`openscad` not installed — skipped the renders."]
    span = footprint(defines)
    h = defines["body_height"]
    asm_h = h + defines["lid_thickness"]
    shots, warns = [], []
    for name, part, cam, cw, cd, ch in (
            ("assembled", "assembled", "0,0,0,60,0,25,0", span, span, asm_h),
            ("parts", "both", "0,0,0,55,0,20,0", 2 * span + 12, span, h)):
        png = out_dir / f"render-{name}.png"
        ok, err = render.shoot_scad_file(
            SCAD, png, dict(defines, render_part=part), cam,
            render.canvas(cw, cd, ch, int(cam.split(",")[3])))
        if ok:
            shots.append(png)
        else:
            warns.append(f"{name} render failed: {err}")
    return shots, warns


# ── the wizard ─────────────────────────────────────────────────────────────
class Wizard(BaseWizard):
    """name -> what's inside -> the rest -> confirm, with `edit`/`advance` detours.

    Same shape as rugged/project by design — each box type owns its own copy of
    the flow so its questions and build stay independently editable.
    """

    COMMAND = COMMAND
    TITLE = TITLE

    def __init__(self, cfg, logger):
        super().__init__(cfg, logger)
        self.idx = 0
        self.params = dict(DEFAULTS)
        self.mode = None             # 'sizes' (compartment-driven) or 'open'
        self.layout_pockets = None   # (pockets, span_w, span_l) — the build form
        self.layout_note = ""
        self.layout = None           # spec.LayoutBuilder while designing the grid
        self.compartment_depth = None
        self.prefilled = set()

    def reset(self):
        super().reset()
        self.idx = 0
        self.params = dict(DEFAULTS)
        self.mode = None
        self.layout_pockets = None
        self.layout_note = ""
        self.layout = None
        self.compartment_depth = None
        self.prefilled = set()

    def start(self) -> str:
        self.reset()
        self.state = "name"
        self.log.info("circular wizard started")
        return (
            f"⭕ **{TITLE} Builder**\n\n"
            "First I'll ask which **insert** you want; that sizes the box. Then "
            "a few questions about the shell, and I build and render it. Reply "
            "`-` to accept a default, `cancel` to stop.\n\n"
            "**Name** — what should I call this box?\n"
            "*(lowercase, hyphens OK — becomes the output folder)*"
        )

    def _steps(self) -> list:
        return [s for s in STEPS if s["key"] not in self.prefilled]

    def _next_question(self) -> str:
        steps = self._steps()
        if self.idx >= len(steps):
            self.state = "confirm"
            return self._summary()
        opt = steps[self.idx]
        hint = f"\n*({opt['hint']})*" if opt.get("hint") else ""
        return (f"**Step {self.idx + 1} of {len(steps)}** — {opt['prompt']}{hint}\n"
                f"Default: `{fmt(self.params[opt['key']])}` — reply `-` to keep it.")

    def _insert_prompt(self, note: str = "") -> str:
        return ((note + "\n\n" if note else "")
                + "**Which insert do you want?** This decides how I size the box.\n"
                "• `cubby` — a divided tray. I'll ask the compartment sizes and "
                "build the box around them *(the tray is rectangular, so the box "
                "grows to clear its corners)*.\n"
                "• `none` — no insert; I'll ask for the interior diameter "
                "instead.")

    def _layout_step(self, text: str):
        res = self.layout.feed(text)
        if res.done:
            b = self.layout
            self.layout_pockets = (b.pockets, b.span_w, b.span_l)
            self.layout_note = b.note
            self.state = "insert_depth"
            return "Layout locked in.\n\n**How deep** should the compartments be? (mm)"
        if res.preview:
            import tempfile
            png = Path(tempfile.gettempdir()) / f"boxbot-layout-{id(self)}.png"
            img = render.layout_png(self.layout.pockets, self.layout.span_w,
                                    self.layout.span_l, png,
                                    title=self.layout.image_title())
            if img:
                return res.reply, [img]
        return res.reply

    def _insert_depth_step(self, text: str) -> str:
        try:
            depth = float(text)
        except ValueError:
            return "Enter the compartment depth in mm, e.g. `40`."
        if not 10 <= depth <= 200:
            return "Depth must be 10–200 mm. Try again."
        self.compartment_depth = depth
        _, span_w, span_l = self.layout_pockets
        self.mode = "sizes"
        interior = spec.interior_from_span(span_w, span_l, depth,
                                           clearance=CASE_CLEARANCE)
        dia = diameter_for_span(interior)
        self.params["Internal_Diameter"] = dia
        self.params["Internal_Height"] = interior["height"]
        self.prefilled = {"Internal_Diameter", "Internal_Height"}
        self.state = "steps"
        self.idx = 0
        note = (f"Your tray is **{interior['width']} × {interior['length']} mm**, "
                f"so the box needs a **{dia} mm** bore to clear its corners "
                f"(interior {dia} × {interior['height']} mm deep).\n\n")
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
            return ("Reply **go** to build, **edit** to change an answer, "
                    "**advance** for more options, or **cancel**.")

        return "Something went wrong — type `cancel` to reset."

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

    # ── advanced options ──────────────────────────────────────────────────
    def _advanced_table(self, note: str = "") -> str:
        rows = [f"{i:>2}  {o['label']:<22} {fmt(self.params[o['key']]):<12} "
                f"{allowed_text(o)}" for i, o in enumerate(ADVANCED, 1)]
        return table("⚙️ **Advanced options**", ("option", "current", "allowed"),
                     rows,
                     "Reply `<number> <value>` to change one *(e.g. `1 3`)*.\n"
                     "Reply `done` when finished, or `cancel` to abort.", note)

    def _advance_step(self, text: str) -> str:
        if text.lower() in ("done", "back", "go", "ok"):
            self.state = "confirm"
            return self._summary()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return self._advanced_table("❌ Use `<number> <value>`, e.g. `1 3`.")
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
        return [None] + self._steps()

    def _edit_table(self, note: str = "") -> str:
        rows = []
        for i, o in enumerate(self._edit_rows(), 1):
            if o is None:
                rows.append(f"{i:>2}  {'Name':<22} {self.name:<12} text")
            else:
                rows.append(f"{i:>2}  {o['key'].replace('_', ' '):<22} "
                            f"{fmt(self.params[o['key']]):<12} {allowed_text(o)}")
        return table("✏️ **Your answers**", ("question", "current", "allowed"),
                     rows,
                     "Reply `<number> <value>` to change one *(e.g. `2 60`)*.\n"
                     "Reply `done` to go back to the summary, or `cancel` to abort.",
                     note)

    def _edit_step(self, text: str) -> str:
        if text.lower() in ("done", "back", "go", "ok"):
            self.state = "confirm"
            return self._summary()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return self._edit_table("❌ Use `<number> <value>`, e.g. `2 60`.")
        rows = self._edit_rows()
        n = int(parts[0])
        if not 1 <= n <= len(rows):
            return self._edit_table(f"❌ Pick a number 1–{len(rows)}.")
        opt = rows[n - 1]

        if opt is None:
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
            return self._edit_table(f"❌ {opt['key'].replace('_', ' ')}: {err}")
        self.params[opt["key"]] = val
        return self._edit_table(f"✅ {opt['key'].replace('_', ' ')} → `{fmt(val)}`")

    # ── confirm + build ───────────────────────────────────────────────────
    def _inside_summary(self) -> str:
        if self.mode == "sizes" and self.layout_pockets:
            return (f"**Insert:** cubby — {self.layout_note}, "
                    f"{self.compartment_depth:g} mm deep\n")
        return "**Insert:** none (open box)\n"

    def _lid_summary(self) -> str:
        p = self.params
        if p["Lid_Style"] == "screw":
            return (f"**Lid:** screw-down, {p['Screw_Count']} ears, "
                    f"{p['Screw_Style']} heads")
        return f"**Lid:** press-fit lip, {p['Lip_Height']:g} mm deep"

    def _summary(self) -> str:
        p = self.params
        changed = [f"{o['label']} = {fmt(p[o['key']])}" for o in ADVANCED
                   if p[o["key"]] != o["default"]]
        extra = ("\n**Advanced:** " + "; ".join(changed)) if changed else ""
        return (
            f"⭕ **{self.name}** — ready to build\n\n"
            + self._inside_summary()
            + f"**Interior:** ⌀{p['Internal_Diameter']:g} × "
            f"{p['Internal_Height']:g} mm deep\n"
            + self._lid_summary()
            + extra + "\n"
            f"📁 `{self.boxes_root() / self.name}`\n\n"
            "**go** to build · **edit** to change any setting above · "
            "**advance** for walls, tolerance & screw ears · **cancel**"
        )

    async def _execute(self):
        name    = self.name
        out_dir = self.boxes_root() / name
        params  = dict(self.params)
        inside  = (self.mode, self.layout_pockets, self.compartment_depth)
        self.reset()

        self.log.info(f"circular build starting: {name} {params}")
        defs = _defines(params)
        result = await asyncio.to_thread(build, defs, out_dir)
        return await asyncio.to_thread(self._finish, name, out_dir, params,
                                       defs, result, inside)

    def _add_cubby(self, params, inside, files, out_dir, warns):
        """Build the divided tray from the listed compartments, if any.

        Built at its natural span — unlike the rectangular boxes there is no
        interior to stretch into, and scaling the tray up to the bore diameter
        would drive its corners into the wall.
        """
        mode, layout, depth = inside
        if mode != "sizes" or not layout:
            return None
        pockets, span_w, span_l = layout
        out = out_dir / "insert.stl"
        ok, err = cubby.build_pockets(pockets, span_w, span_l,
                                      spec.tray_height(depth), out,
                                      corner_radius=0.0)
        if not ok:
            warns.append(f"insert tray failed to build: {err}")
            return None
        files["insert"] = str(out)
        n = len(pockets)
        return f"custom tray, {n} compartment{'s' if n != 1 else ''}"

    def _finish(self, name, out_dir, params, defs, result, inside):
        status = result.get("status")
        if status in ("unavailable", "render_failed"):
            errs = "\n".join(f"  • {e}" for e in result.get("errors", []))
            return (f"❌ **{name}** couldn't be built:\n{errs}\n\n"
                    f"Nothing was saved. Run `/{COMMAND}` again.")

        files = dict(result.get("files", {}))
        warns = []
        cubby_note = self._add_cubby(params, inside, files, out_dir, warns)
        shots, rwarns = render_shots(defs, out_dir)
        warns += rwarns

        span = footprint(defs)
        if span > BED_MM:
            warns.append(f"{span:.0f} mm across may not fit a {BED_MM} mm bed — "
                         "check before printing.")

        boxmeta.write(out_dir, _meta(name, params))
        self.log.info(f"circular build finished: {name} shots={len(shots)} "
                      f"cubby={cubby_note}")

        lines = [
            f"✅ **{name}** — built",
            "",
            f"**Interior:** ⌀{params['Internal_Diameter']:g} × "
            f"{params['Internal_Height']:g} mm deep",
            f"**Outer:** ⌀{defs['outer_dia']:.0f} × "
            f"{defs['body_height'] + defs['lid_thickness']:.0f} mm tall",
            "**Parts:** 1× body, 1× lid",
        ]
        if params["Lid_Style"] == "screw":
            lines.append(f"**Hardware:** {params['Screw_Count']} × M3 "
                         f"self-tapping screws ({params['Screw_Style']} heads)")
        else:
            lines.append("**Hardware:** none — the lid presses on")
        lines += [
            "**Print:** 0.2 mm layers, no supports (Prusa Core One, 0.4 mm nozzle)",
            "",
            f"📁 `{out_dir}`",
        ]
        if cubby_note:
            lines.insert(5, f"**Insert:** {cubby_note}")
        for w in warns:
            lines.append(f"⚠️ {w}")
        return "\n".join(lines), shots
