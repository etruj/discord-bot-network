#!/usr/bin/env python3
"""Project box — an open tray body with a flat, screw-down lid.

Owns its whole wizard, like every box type. The screw-close closure is the
fourth core shape from the original design notes: a general-purpose enclosure, distinct from
rugged's hinged clamshell and sliding's groove lid. Geometry is a simplified
working copy of pbtec's "Easy Project Box" (CC BY-NC-SA 4.0, non-commercial:
boxes this command generates are for non-commercial use only) — see
`scad/project_box.scad` and `scad/project_box.LICENSE.md`.

Same interior-first shape as rugged: name -> optional cubby insert (sizes the
box) -> shell questions -> confirm. Unlike rugged there's no lid depth to ask
— the lid is flat and contributes no cavity depth, so the interior height
*is* the compartment depth when a cubby is chosen.
"""
import asyncio
from pathlib import Path

import boxmeta
import cubby
import render
import spec
from wizard_base import (BaseWizard, allowed_text, fmt, parse_answer,
                         sanitize_name, table)

COMMAND     = "project"
TITLE       = "Project Box"
DESCRIPTION = "Design a 3D-printable screw-close project box (wizard)"

SCAD = Path(__file__).resolve().parent.parent / "scad" / "project_box.scad"

# Fixed engineering values the wizard doesn't ask about — an M3 self-tapping
# boss sized for PLA, per the draft's own head/hole reference table.
HOLE_DIA_LID    = 3.4    # M3 clearance through the lid
HOLE_DIA_THREAD = 2.5    # M3 self-tap pilot into the boss
SCREW_POST_DIA  = 8.0
HOLE_POS        = 7.0    # boss centre, inset from each outer wall face
CS_HEAD_DIA     = 5.5    # M3 countersink head
NS_HEAD_DIA     = 6.0    # M3 pan head
NS_HEAD_HIGH    = 2.5
MIN_BOTTOM      = 2.0    # mm of post left below the pilot hole, minimum
HOLE_DEEPNESS_DEFAULT = 12.0
# Above this wall span, add 2 more bosses along that side so a long lid can't
# bow between the corners — an engineering call, not a wizard question.
MID_SCREW_SPAN = 150.0

# Tray-to-case gap per side when a cubby insert is chosen — matches rugged's.
CASE_CLEARANCE = 0.15

SCREW_STYLES = {"flush": 1, "raised": 2, "hole-only": 0}

STEPS = [
    dict(key="Internal_Width", kind="int", lo=20, hi=300, default=100,
         prompt="Interior **width** — side-to-side (X), mm", hint="20–300"),
    dict(key="Internal_Length", kind="int", lo=20, hi=300, default=140,
         prompt="Interior **length** — front-to-back (Y), mm", hint="20–300"),
    dict(key="Internal_Height", kind="int", lo=10, hi=200, default=40,
         prompt="Interior **depth** (Z), mm", hint="10–200"),
]

ADVANCED = [
    dict(key="Wall_Thickness", kind="float", lo=1.5, hi=10, default=3.0,
         label="Wall thickness (mm)"),
    dict(key="Lid_Thickness", kind="float", lo=1.5, hi=8, default=3.0,
         label="Lid thickness (mm)"),
    dict(key="Tolerance", kind="float", lo=0.05, hi=1.0, default=0.2,
         label="Lid fit tolerance (mm)"),
    dict(key="Corner_Radius", kind="float", lo=0, hi=25, default=5.0,
         label="Corner radius (mm)"),
    dict(key="Screw_Style", kind="choice", default="flush",
         choices=list(SCREW_STYLES), label="Screw head style"),
]

DEFAULTS = {o["key"]: o["default"] for o in STEPS + ADVANCED}
PART_ORDER = ("body", "lid")


# ── interior -> outer + SCAD defines ───────────────────────────────────────
def _defines(p: dict) -> dict:
    """Turn interior answers into the SCAD's outer-dimension parameters.

    Inverts the SCAD's own interior derivation (cavity = body height minus the
    floor, lid contributes no depth since it sits flush on top of the walls):
        inner_h = box_height - wall_thickness
    so the box ends up with exactly the requested usable depth.
    """
    wall = p["Wall_Thickness"]
    box_height = p["Internal_Height"] + wall
    hole_deepness = min(HOLE_DEEPNESS_DEFAULT, max(4.0, box_height - MIN_BOTTOM))
    return {
        "box_width": round(p["Internal_Width"] + 2 * wall, 3),
        "box_length": round(p["Internal_Length"] + 2 * wall, 3),
        "box_height": round(box_height, 3),
        "corner_radius": p["Corner_Radius"],
        "wall_thickness": wall,
        "lid_thickness": p["Lid_Thickness"],
        "tolerance": p["Tolerance"],
        "hole_dia_lid": HOLE_DIA_LID,
        "hole_dia_thread": HOLE_DIA_THREAD,
        "hole_deepness": round(hole_deepness, 3),
        "screw_post_dia": SCREW_POST_DIA,
        "hole_pos": HOLE_POS,
        "add_x_mid": p["Internal_Length"] + 2 * wall > MID_SCREW_SPAN,
        "add_y_mid": p["Internal_Width"] + 2 * wall > MID_SCREW_SPAN,
        "hole_type": SCREW_STYLES[p["Screw_Style"]],
        "cs_head_dia": CS_HEAD_DIA,
        "ns_head_dia": NS_HEAD_DIA,
        "ns_head_high": NS_HEAD_HIGH,
    }


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
    w, l, h = defines["box_width"], defines["box_length"], defines["box_height"]
    asm_h = h + defines["lid_thickness"]
    shots, warns = [], []
    for name, part, cam, cw, cd, ch in (
            ("assembled", "assembled", "0,0,0,60,0,25,0", w, l, asm_h),
            ("parts", "both", "0,0,0,55,0,20,0", 2 * w + 15, l, h)):
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

    Same shape as rugged/sliding by design — each box type owns its own copy
    of the flow so its questions and build stay independently editable.
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
        self.log.info("project wizard started")
        return (
            f"🧰 **{TITLE} Builder**\n\n"
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
                "build the box around them.\n"
                "• `none` — no insert; I'll ask for the interior dimensions "
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
        self.params["Internal_Width"] = interior["width"]
        self.params["Internal_Length"] = interior["length"]
        self.params["Internal_Height"] = interior["height"]
        self.prefilled = {"Internal_Width", "Internal_Length", "Internal_Height"}
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
                     "Reply `<number> <value>` to change one *(e.g. `1 4`)*.\n"
                     "Reply `done` when finished, or `cancel` to abort.", note)

    def _advance_step(self, text: str) -> str:
        if text.lower() in ("done", "back", "go", "ok"):
            self.state = "confirm"
            return self._summary()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return self._advanced_table("❌ Use `<number> <value>`, e.g. `1 4`.")
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

    def _summary(self) -> str:
        p = self.params
        changed = [f"{o['label']} = {fmt(p[o['key']])}" for o in ADVANCED
                   if p[o["key"]] != o["default"]]
        extra = ("\n**Advanced:** " + "; ".join(changed)) if changed else ""
        return (
            f"🧰 **{self.name}** — ready to build\n\n"
            + self._inside_summary()
            + f"**Interior:** {p['Internal_Width']:g} × {p['Internal_Length']:g} × "
            f"{p['Internal_Height']:g} mm\n"
            f"**Lid:** screw-down, {p['Screw_Style']} heads"
            + extra + "\n"
            f"📁 `{self.boxes_root() / self.name}`\n\n"
            "**go** to build · **edit** to change any setting above · "
            "**advance** for walls, tolerance & screw style · **cancel**"
        )

    async def _execute(self):
        name    = self.name
        out_dir = self.boxes_root() / name
        params  = dict(self.params)
        inside  = (self.mode, self.layout_pockets, self.compartment_depth)
        self.reset()

        self.log.info(f"project build starting: {name} {params}")
        defs = _defines(params)
        result = await asyncio.to_thread(build, defs, out_dir)
        return await asyncio.to_thread(self._finish, name, out_dir, params,
                                       defs, result, inside)

    def _add_cubby(self, params, inside, files, out_dir, warns):
        """Build the divided tray from the listed compartments, if any."""
        mode, layout, depth = inside
        if mode != "sizes" or not layout:
            return None
        pockets, span_w, span_l = layout
        fitted, fw, fl = cubby.fit_pockets(
            params["Internal_Width"], params["Internal_Length"], pockets,
            span_w, span_l, CASE_CLEARANCE)
        out = out_dir / "insert.stl"
        ok, err = cubby.build_pockets(
            fitted, fw, fl, spec.tray_height(depth), out,
            corner_radius=max(0.0, params["Corner_Radius"] - CASE_CLEARANCE))
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

        boxmeta.write(out_dir, boxmeta.make(
            name, "project",
            {"width": params["Internal_Width"],
             "length": params["Internal_Length"],
             "height": params["Internal_Height"]},
            insert_supported=True,
            corner_radius=params["Corner_Radius"]))
        self.log.info(f"project build finished: {name} shots={len(shots)} "
                      f"cubby={cubby_note}")

        n_screws = 4 + (2 if defs["add_x_mid"] else 0) + (2 if defs["add_y_mid"] else 0)
        lines = [
            f"✅ **{name}** — built",
            "",
            f"**Interior:** {params['Internal_Width']:g} × "
            f"{params['Internal_Length']:g} × {params['Internal_Height']:g} mm",
            f"**Outer:** {defs['box_width']:.0f} × {defs['box_length']:.0f} × "
            f"{defs['box_height'] + defs['lid_thickness']:.0f} mm",
            "**Parts:** 1× body, 1× lid",
            f"**Hardware:** {n_screws} × M3 self-tapping screws "
            f"({params['Screw_Style']} heads)",
            "**Print:** 0.2 mm layers, no supports (Prusa Core One, 0.4 mm nozzle)",
            "",
            f"📁 `{out_dir}`",
        ]
        if cubby_note:
            lines.insert(4, f"**Insert:** {cubby_note}")
        for w in warns:
            lines.append(f"⚠️ {w}")
        return "\n".join(lines), shots
