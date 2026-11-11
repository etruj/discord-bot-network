#!/usr/bin/env python3
"""Sliding-lid compartment box — one printed body with a lid that slides into a
groove, divided into a grid of compartments.

Owns its whole wizard, like every box type. Unlike the raw
`scad/sliding_box.scad` (which is outer-dimension driven, the way a Customizer
user thinks), this wizard is driven by the **interior** the user needs and
computes the outer box around it — the project's core "describe the space, not
the shell" principle. The inversion lives in `_defines()`.

Geometry and previews both come from `scad/sliding_box.scad` via `render`:
`build_stl` for the two STLs (`render_part=box|lid`) and `shoot_scad_file` for
the assembled and parts previews, reusing the SCAD's own layout rather than
re-composing meshes.
"""
import asyncio
import tempfile
from pathlib import Path

import boxmeta
import render
import spec
from wizard_base import (BaseWizard, allowed_text, fmt, parse_answer,
                         sanitize_name, table)

COMMAND     = "sliding"
TITLE       = "Sliding-Lid Box"
DESCRIPTION = "Design a 3D-printable sliding-lid compartment box (wizard)"

SCAD = Path(__file__).resolve().parent.parent / "scad" / "sliding_box.scad"

# Fixed engineering values the wizard doesn't ask about. They mirror the SCAD's
# own defaults and feed the interior->outer height math below.
GROOVE_MARGIN_TOP = 2.0     # gap from the top edge down to the lid groove
DIVIDER_CLEARANCE = 0.2     # gap between divider tops and the sliding lid
GROOVE_DEPTH      = 1.5
COMPARTMENT_CHAMFER = 2.5
TEXT_SIZE         = 6

STEPS = [
    dict(key="Internal_Width", kind="int", lo=20, hi=300, default=120,
         prompt="Interior **width** — side-to-side (X), mm", hint="20–300"),
    dict(key="Internal_Length", kind="int", lo=20, hi=300, default=160,
         prompt="Interior **length** — the way the lid slides (Y), mm",
         hint="20–300"),
    dict(key="Internal_Height", kind="int", lo=5, hi=200, default=30,
         prompt="Interior **depth** (Z), mm", hint="5–200"),
    dict(key="Compartments_Across", kind="int", lo=1, hi=10, default=2,
         prompt="Compartments **across** the width", hint="1–10"),
    dict(key="Compartments_Along", kind="int", lo=1, hi=10, default=3,
         prompt="Compartments **along** the length", hint="1–10"),
]

# Not asked — defaulted, shown in the summary, changed with `edit`. In `custom`
# mode the compartments answer every STEP above, so leaving the engraving out of
# the linear flow is what takes that path straight from depth to confirm.
OPTIONS = [
    dict(key="Lid_Text", kind="str", default="", label="Lid engraving"),
]

ADVANCED = [
    dict(key="Wall_Thickness", kind="float", lo=1.5, hi=10, default=3.0,
         label="Wall thickness (mm)"),
    dict(key="Floor_Thickness", kind="float", lo=1.5, hi=10, default=3.0,
         label="Floor thickness (mm)"),
    dict(key="Lid_Thickness", kind="float", lo=1.5, hi=6, default=2.5,
         label="Lid thickness (mm)"),
    dict(key="Tolerance", kind="float", lo=0.1, hi=1.0, default=0.3,
         label="Slide tolerance (mm)"),
    dict(key="Corner_Radius", kind="float", lo=0, hi=25, default=8.0,
         label="Corner radius (mm)"),
    dict(key="Divider_Thickness", kind="float", lo=0.8, hi=6, default=1.5,
         label="Divider thickness (mm)"),
    dict(key="Split_Ratio", kind="float", lo=0.5, hi=0.9, default=0.75,
         label="Lid/front split ratio"),
]

DEFAULTS = {o["key"]: o["default"] for o in STEPS + OPTIONS + ADVANCED}
PART_ORDER = ("body", "lid")
# Roughly the Prusa Core One usable bed; a soft warning, not a hard stop.
BED_MM = 250


MAX_TEXT_CHARS = 12          # absolute cap; the fit check is the real limit


# ── lid-text fit ────────────────────────────────────────────────────────────
# OpenSCAD renders `text(size=s)` about 1.385× larger than PIL's pixel-size
# basis for the same nominal size — a stable, string-independent ratio measured
# against the real `openscad` binary for Liberation Sans Bold (the font the SCAD
# hardcodes). It's a size-basis difference, not a metrics quirk, so one constant
# covers every string. Without it the estimate ran ~40% short and long labels
# sailed past the fit check and overshot the lid.
OPENSCAD_TEXT_SCALE = 1.385


def _text_length_mm(text: str, size: float = TEXT_SIZE) -> float:
    """Estimate the engraved text's rendered length (mm), matching OpenSCAD.

    Uses the font's **advance width** (`getlength`) — what OpenSCAD actually lays
    text out by — not the tight ink box (`getbbox`), which undercounts side
    bearings, then applies `OPENSCAD_TEXT_SCALE` for OpenSCAD's size basis. The
    result tracks the rendered length within ~1%.
    """
    try:
        from PIL import ImageFont
    except ImportError:
        return len(text) * 0.85 * size                 # rough, deliberately high
    font = None
    for name in ("LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, 100)
            break
        except OSError:
            continue
    if font is None:
        return len(text) * 0.85 * size
    return font.getlength(text) / 100.0 * size * OPENSCAD_TEXT_SCALE


def _text_available_mm(p: dict) -> float:
    """The lid length the engraving must fit into (it runs along the slide axis)."""
    box_length = p["Internal_Length"] + 2 * p["Wall_Thickness"]
    split_y = box_length * p["Split_Ratio"]
    return split_y - p["Wall_Thickness"] - 4.0         # ~2 mm margin each end


def text_fits(text: str, p: dict) -> tuple:
    """(fits, max_chars) for `text` engraved on this box's lid at the current size."""
    avail = _text_available_mm(p)
    length = _text_length_mm(text)
    if length <= avail:
        return True, len(text)
    per_char = length / max(1, len(text))
    return False, max(0, int(avail / per_char))


# ── interior -> outer + SCAD defines ───────────────────────────────────────
def _defines(p: dict, layout=None) -> dict:
    """Turn interior answers into the SCAD's outer-dimension parameters.

    Inverts the SCAD's own interior derivation
        inner_h = box_height - groove_margin_top - lid_thickness
                  - floor - divider_clearance
    so the box ends up with exactly the requested usable depth.

    With `layout` (pockets, span_w, span_l) — the compartment-driven mode — the
    compartments *are* the interior (no removable tray, no clearance): the span
    is the interior and the exact pockets drive the SCAD's dividers, which lets
    ragged/odd layouts through. Without it, the box is the requested interior
    split into an even grid via compartments_x/y.
    """
    wall = p["Wall_Thickness"]
    div = p["Divider_Thickness"]
    box_height = (p["Internal_Height"] + GROOVE_MARGIN_TOP + p["Lid_Thickness"]
                  + p["Floor_Thickness"] + DIVIDER_CLEARANCE)
    text = (p["Lid_Text"] or "").strip()
    if layout:
        pockets, inner_w, inner_l = layout
    else:
        pockets, inner_w, inner_l = [], p["Internal_Width"], p["Internal_Length"]
    return {
        "box_width": round(inner_w + 2 * wall, 3),
        "box_length": round(inner_l + 2 * wall, 3),
        "box_height": round(box_height, 3),
        "box_radius": p["Corner_Radius"],
        "split_ratio": p["Split_Ratio"],
        "enable_text": bool(text),
        "lid_text": text or " ",
        "text_size": TEXT_SIZE,
        "compartments_x": p["Compartments_Across"],
        "compartments_y": p["Compartments_Along"],
        "col_widths": [],
        "row_lengths": [],
        "pockets": [[round(v, 3) for v in pk] for pk in pockets],
        "divider_thickness": div,
        "compartment_chamfer": COMPARTMENT_CHAMFER,
        "divider_clearance": DIVIDER_CLEARANCE,
        "wall_thickness": wall,
        "floor_thickness": p["Floor_Thickness"],
        "lid_thickness": p["Lid_Thickness"],
        "groove_depth": GROOVE_DEPTH,
        "groove_margin_top": GROOVE_MARGIN_TOP,
        "tolerance": p["Tolerance"],
    }


def build(defines: dict, out_dir: Path) -> dict:
    """Render the body and lid STLs. Returns a result dict shaped like rugged's."""
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
    shots, warns = [], []
    for name, part, cam, cw, cd in (
            ("assembled", "assembled", "0,0,0,62,0,25,0", w, l),
            ("parts", "both", "0,0,0,58,0,18,0", 3 * w + 15, l)):
        png = out_dir / f"render-{name}.png"
        ok, err = render.shoot_scad_file(
            SCAD, png, dict(defines, render_part=part), cam,
            render.canvas(cw, cd, h, int(cam.split(",")[3])))
        if ok:
            shots.append(png)
        else:
            warns.append(f"{name} render failed: {err}")
    return shots, warns


# ── the wizard ─────────────────────────────────────────────────────────────
class Wizard(BaseWizard):
    """name → 6 questions → confirm, with `edit` and `advance` detours.

    Same shape as the rugged wizard by design — each box type owns its own copy
    of the flow so its questions and build stay independently editable.
    """

    COMMAND = COMMAND
    TITLE = TITLE

    def __init__(self, cfg, logger):
        super().__init__(cfg, logger)
        self.idx = 0
        self.params = dict(DEFAULTS)
        self.mode = None             # 'sizes' (compartment-driven) or 'open'
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
        self.log.info("sliding wizard started")
        return (
            f"🗄️ **{TITLE} Builder**\n\n"
            "First I'll ask what compartments you want inside; that sizes the "
            "box. Then I build it and render it. Reply `-` to accept a default, "
            "`cancel` to stop.\n\n"
            "**Name** — what should I call this box?\n"
            "*(lowercase, hyphens OK — becomes the output folder)*"
        )

    def _steps(self) -> list:
        """Live steps, minus any answered by the compartment-driven opening."""
        return [s for s in STEPS if s["key"] not in self.prefilled]

    def _options(self) -> list:
        """Settings that are defaulted rather than asked, but stay editable."""
        return list(OPTIONS)

    def _next_question(self) -> str:
        steps = self._steps()
        if self.idx >= len(steps):
            self.state = "confirm"
            return self._summary()
        opt = steps[self.idx]
        hint = f"\n*({opt['hint']})*" if opt.get("hint") else ""
        return (f"**Step {self.idx + 1} of {len(steps)}** — {opt['prompt']}{hint}\n"
                f"Default: `{fmt(self.params[opt['key']]) or '(none)'}` — "
                "reply `-` to keep it.")

    def _insert_prompt(self, note: str = "") -> str:
        return ((note + "\n\n" if note else "")
                + "**How do you want the compartments?** This sizes the box.\n"
                "• `custom` — list exact compartment sizes; I build the box "
                "around them.\n"
                "• `even` — I'll ask the interior and split it into an even grid.")

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
                if opt["key"] == "Lid_Text":
                    err = self._check_lid_text(val)
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
            shortcut = self._confirm_shortcut(text)
            if shortcut is not None:
                return shortcut
            return ("Reply **go** to build, **edit** to change a setting, "
                    "**advance** for more options, or **cancel**.")

        return "Something went wrong — type `cancel` to reset."

    # ── one-word shortcuts from the summary ───────────────────────────────
    def _confirm_shortcut(self, text: str):
        """Make the summary's `text …` suggestion literally typeable.

        The engraving isn't asked about any more, so without this it costs
        `edit` → find the row → `done`. Returns None when the text isn't a
        shortcut, leaving the normal confirm help to handle it. The same fit
        check runs here as everywhere else, so a too-long label is still caught
        rather than silently cropped on the lid.
        """
        parts = text.split(None, 1)
        word = parts[0].lower()
        arg = (parts[1].strip() if len(parts) > 1 else "")
        if word not in ("text", "engrave", "engraving", "label"):
            return None
        if arg.lower() in ("off", "none", "no", "clear", "remove"):
            self.params["Lid_Text"] = ""
            return "✅ Lid engraving cleared.\n\n" + self._summary()
        if not arg:
            return ("What should the lid say? Reply `text TOOLBOX` "
                    "(or `text off` to clear it).")
        err = self._check_lid_text(arg)
        if err:
            return "❌ " + err + "\n\n" + self._summary()
        self.params["Lid_Text"] = arg
        return f"✅ Lid engraving → **“{arg}”**\n\n" + self._summary()

    # ── compartment-first: the compartments drive the box ─────────────────
    def _check_lid_text(self, val) -> str:
        """Return an error if the lid text is too long for this box, else ''.

        Enforced at entry (and again at build) so the engraving never silently
        overruns the lid — the old bug that cut labels off on small boxes.
        """
        v = (val or "").strip()
        if not v:
            return ""
        if len(v) > MAX_TEXT_CHARS:
            return (f"That's {len(v)} characters — keep the lid text to "
                    f"{MAX_TEXT_CHARS} or fewer, or `text off` for none.")
        fits, max_chars = text_fits(v, self.params)
        if not fits:
            return (f"`{v}` won't fit this box's lid — about **{max_chars}** "
                    f"character{'s' if max_chars != 1 else ''} fit at this size. "
                    "Try a shorter label, a longer box, or `text off` for none.")
        return ""

    def _insert_step(self, text: str):
        low = text.lower()
        if low in ("custom", "cubby", "sizes", "list", "1"):
            self.layout = spec.LayoutBuilder()
            self.state = "layout"
            return self.layout.prompt()
        if low in ("even", "open", "none", "no", "2"):
            self.mode = "open"
            self.state = "steps"
            self.idx = 0
            return ("Even grid — I'll ask for the interior.\n\n"
                    + self._next_question())
        return self._insert_prompt("❌ Reply `custom` to design the compartments, "
                                   "or `even` for an even grid.")

    def _layout_step(self, text: str):
        """Delegate the grid design to the shared LayoutBuilder, rendering a
        preview image whenever it asks, until the layout is confirmed."""
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

    def _insert_depth_step(self, text: str) -> str:
        try:
            depth = float(text)
        except ValueError:
            return "Enter the compartment depth in mm, e.g. `30`."
        if not 5 <= depth <= 200:
            return "Depth must be 5–200 mm. Try again."
        self.compartment_depth = depth
        # The compartments ARE the interior — no tray, no clearance. The span
        # (cells + dividers) is exactly the interior.
        _, span_w, span_l = self.layout_pockets
        self.mode = "sizes"
        self.params["Internal_Width"] = round(span_w, 2)
        self.params["Internal_Length"] = round(span_l, 2)
        self.params["Internal_Height"] = depth
        self.params["Compartments_Across"] = 1
        self.params["Compartments_Along"] = len(self.layout_pockets[0])
        self.prefilled = {"Internal_Width", "Internal_Length", "Internal_Height",
                          "Compartments_Across", "Compartments_Along"}
        self.state = "steps"
        self.idx = 0
        note = (f"Sized the interior to **{self.params['Internal_Width']:g} × "
                f"{self.params['Internal_Length']:g} × {depth:g} mm** around your "
                f"compartments.\n\n")
        return note + self._next_question()

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
        # Name, the asked questions, then the defaulted OPTIONS — those are only
        # reachable here, so they have to be listed.
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
                            f"{fmt(self.params[o['key']]) or '(none)':<12} "
                            f"{allowed_text(o)}")
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

        if opt is None:                              # renaming the box
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
        if opt["key"] == "Lid_Text":
            terr = self._check_lid_text(val)
            if terr:
                return self._edit_table("❌ " + terr)
        self.params[opt["key"]] = val
        return self._edit_table(f"✅ {self._row_label(opt)} → `{fmt(val)}`")

    # ── confirm + build ───────────────────────────────────────────────────
    def _summary(self) -> str:
        p = self.params
        changed = [f"{o['label']} = {fmt(p[o['key']])}" for o in ADVANCED
                   if p[o["key"]] != o["default"]]
        text = (p["Lid_Text"] or "").strip()
        if self.mode == "sizes" and self.layout_pockets:
            compartments = (f"**Compartments:** {self.layout_note} "
                            f"({self.compartment_depth:g} mm deep)")
        else:
            compartments = (f"**Compartments:** {p['Compartments_Across']} across × "
                            f"{p['Compartments_Along']} along "
                            f"= {p['Compartments_Across'] * p['Compartments_Along']}")
        lines = [
            f"🗄️ **{self.name}** — ready to build",
            "",
            f"**Interior:** {p['Internal_Width']:g} × {p['Internal_Length']:g} × "
            f"{p['Internal_Height']:g} mm",
            compartments,
        ]
        # Shown even when empty, with the words that set it: the engraving is no
        # longer asked about, so this line is the only place a user learns the
        # option exists — and "somewhere under edit" is not discoverable.
        lines.append(f"✏️ **Lid engraving:** “{text}” — reply `text off` to clear"
                     if text else
                     "✏️ **Lid engraving:** none — reply `text TOOLBOX` to add one")
        if changed:
            lines.append("**Advanced:** " + "; ".join(changed))
        lines += [
            f"📁 `{self.boxes_root() / self.name}`",
            "",
            "**go** to build · **edit** to change any setting above · "
            "**advance** for walls, tolerance & corners · **cancel**",
        ]
        return "\n".join(lines)

    async def _execute(self):
        # Final guard: advanced changes (split ratio, wall) can shrink the lid
        # after the text was accepted, so re-check before building.
        terr = self._check_lid_text(self.params.get("Lid_Text"))
        if terr:
            return ("❌ " + terr + "\n\nReply **edit** to change the lid text, "
                    "**advance** to widen the box, or **cancel**.")

        name    = self.name
        out_dir = self.boxes_root() / name
        params  = dict(self.params)
        layout  = self.layout_pockets if self.mode == "sizes" else None
        self.reset()

        self.log.info(f"sliding build starting: {name} {params} layout={layout}")
        defs = _defines(params, layout)
        result = await asyncio.to_thread(build, defs, out_dir)
        return await asyncio.to_thread(self._finish, name, out_dir, params,
                                       defs, result)

    def _finish(self, name, out_dir, params, defs, result):
        status = result.get("status")
        if status in ("unavailable", "render_failed"):
            errs = "\n".join(f"  • {e}" for e in result.get("errors", []))
            return (f"❌ **{name}** couldn't be built:\n{errs}\n\n"
                    f"Nothing was saved. Run `/{COMMAND}` again.")

        shots, warns = render_shots(defs, out_dir)
        boxmeta.write(out_dir, boxmeta.make(
            name, "sliding",
            {"width": params["Internal_Width"],
             "length": params["Internal_Length"],
             "height": params["Internal_Height"]},
            # Compartments are integral to a sliding box, so it isn't an
            # /inserts target — its cavity is already divided.
            insert_supported=False,
            corner_radius=params["Corner_Radius"]))
        self.log.info(f"sliding build finished: {name} shots={len(shots)}")

        if max(defs["box_width"], defs["box_length"]) > BED_MM:
            warns.append(f"outer {defs['box_width']:.0f}×{defs['box_length']:.0f} mm "
                         f"may not fit a {BED_MM} mm bed — check before printing.")

        lines = [
            f"✅ **{name}** — built",
            "",
            f"**Interior:** {params['Internal_Width']} × {params['Internal_Length']} × "
            f"{params['Internal_Height']} mm  "
            f"({params['Compartments_Across']}×{params['Compartments_Along']} cells)",
            f"**Outer:** {defs['box_width']:.0f} × {defs['box_length']:.0f} × "
            f"{defs['box_height']:.0f} mm",
            "**Parts:** 1× body, 1× lid",
            "**Print:** 0.2 mm layers, no supports (Prusa Core One, 0.4 mm nozzle)",
            "",
            f"📁 `{out_dir}`",
        ]
        for w in warns:
            lines.append(f"⚠️ {w}")
        return "\n".join(lines), shots
