#!/usr/bin/env python3
"""`/inserts` — build a cubby tray for a box that already exists.

This is the reverse of the box wizards: rather than "build a box around what I
need", it's "I already have this box — build something for the inside". The box
is chosen from a Discord dropdown (autocomplete over `boxmeta.scan`), so only
boxes that carry a `box.json` and are marked insert-ready ever appear.

It is *not* a box type and is deliberately absent from `box_types.TYPES`: it
generates nothing new on its own, it reads an existing box's interior and drops
a tray into that same folder. gutsman registers it by hand (like `/debug`),
seeding the wizard with the chosen box via `start(box_name)`.

Cubby only for now — custom-SVG inserts are a later, larger pass.
"""
import asyncio
from pathlib import Path

import boxmeta
import cubby
import render
from wizard_base import BaseWizard

COMMAND     = "inserts"
TITLE       = "Cubby Insert"
DESCRIPTION = "Build a divided cubby tray for an existing box"


def choices(cfg) -> list:
    """Insert-ready box names for the `/inserts` autocomplete, newest first."""
    return [m["name"] for m in boxmeta.scan(cfg["boxes_root"], insert_only=True)]


def render_tray(stl: Path, out_dir: Path):
    """A single flat shot of the tray. Returns (shots, warnings)."""
    if not render.have_openscad():
        return [], ["`openscad` not installed — skipped the render."]
    meshes, bounds, warns = render.load_bounds({"insert": str(stl)})
    if "insert" not in bounds:
        return [], warns + ["couldn't read the tray STL — skipped the render."]
    src, w, d, h = render.parts_grid({"insert": str(stl)}, bounds, ["insert"])
    png = out_dir / "render-insert.png"
    ok, err = render.shoot(src, png, "0,0,0,58,0,25,0", render.canvas(w, d, h, 58))
    return ([png] if ok else []), (warns if ok else warns + [f"render failed: {err}"])


class Wizard(BaseWizard):
    """box (via the command) → layout → clearance → height → confirm → build.

    Seeded by `start(box_name)`; every other wizard starts from a slash command
    with no argument, so this one carries the selected box in as state.
    """

    COMMAND = COMMAND
    TITLE = TITLE

    def __init__(self, cfg, logger):
        super().__init__(cfg, logger)
        self.meta = None
        self.box_dir = None
        self.cols = self.rows = None
        self.clearance = cubby.CLEARANCES["standard"]
        self.height = None

    def reset(self):
        super().reset()
        self.meta = None
        self.box_dir = None
        self.cols = self.rows = None
        self.clearance = cubby.CLEARANCES["standard"]
        self.height = None

    # ── entry: seeded with the chosen box ─────────────────────────────────
    def start(self, box_name: str) -> str:
        self.reset()
        meta = boxmeta.read(Path(self.cfg["boxes_root"]) / box_name)
        if not meta:
            return (f"❌ I don't have metadata for `{box_name}`. Pick a box from "
                    "the dropdown — only insert-ready boxes are listed.")
        if not meta.get("insert", {}).get("supported"):
            return f"❌ `{box_name}` isn't marked insert-ready."
        self.meta = meta
        self.name = box_name
        self.box_dir = Path(self.cfg["boxes_root"]) / box_name
        self.height = float(meta["internal"]["height"])
        self.state = "layout"
        self.log.info(f"inserts wizard started for {box_name}")
        return f"🧩 **Cubby insert for `{box_name}`**\n\n" + self._layout_prompt()

    def _interior(self) -> tuple:
        i = self.meta["internal"]
        return float(i["width"]), float(i["length"]), float(i["height"])

    def _layout_prompt(self, note: str = "") -> str:
        w, l, h = self._interior()
        feas = cubby.feasible_counts(w, l)
        offer = ", ".join(str(n) for n in feas) or "(box too small for a grid)"
        return ((note + "\n\n" if note else "")
                + f"**Interior:** {w:g} × {l:g} × {h:g} mm\n\n"
                "How should I divide it?\n"
                f"• a **count** — one of: {offer}\n"
                "• or a **grid**, e.g. `grid 3 2` (cols along width × rows along length)\n\n"
                "Reply with your choice, or `cancel`.")

    # ── conversation ──────────────────────────────────────────────────────
    async def step(self, message: str):
        text = message.strip()
        low = text.lower()
        if low == "cancel":
            return self.cancel()

        if self.state == "layout":
            return self._pick_layout(text)
        if self.state == "clearance":
            return self._pick_clearance(text)
        if self.state == "height":
            return self._pick_height(text)
        if self.state == "confirm":
            if low in ("go", "yes", "y", "build", "ok", "yep", "do it"):
                return await self._execute()
            if low in ("edit", "back", "change"):
                self.state = "layout"
                return self._layout_prompt()
            if low in ("no", "n", "abort"):
                return self.cancel()
            return "Reply **go** to build, **edit** to start over, or **cancel**."
        return "Something went wrong — type `cancel` to reset."

    def _pick_layout(self, text: str) -> str:
        w, l, _ = self._interior()
        parts = text.split()
        if parts and parts[0].lower() == "grid":
            if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
                return self._layout_prompt("❌ Use `grid <cols> <rows>`, e.g. `grid 3 2`.")
            cols, rows = int(parts[1]), int(parts[2])
        elif len(parts) == 1 and parts[0].isdigit():
            cols, rows = cubby.arrange(int(parts[0]), w, l)
        else:
            return self._layout_prompt("❌ Reply with a count (e.g. `4`) or a grid "
                                       "(e.g. `grid 3 2`).")
        if cols < 1 or rows < 1:
            return self._layout_prompt("❌ Need at least one compartment.")
        if not cubby.cell_ok(cols, rows, w, l):
            cw, cl = cubby.cell_size(cols, rows, w, l)
            return self._layout_prompt(
                f"❌ That leaves {cw:.0f} × {cl:.0f} mm compartments — under the "
                f"{cubby.MIN_CELL:g} mm minimum. Try fewer.")
        self.cols, self.rows = cols, rows
        self.state = "clearance"
        return (f"✅ Layout: **{cols} × {rows}** "
                f"({cols * rows} compartments).\n\n" + self._clearance_prompt())

    def _clearance_prompt(self, note: str = "") -> str:
        rows = "\n".join(f"  {name:<9} {mm:.2f} mm per side"
                         for name, mm in cubby.CLEARANCES.items())
        return ((note + "\n\n" if note else "")
                + "🔧 **Fit clearance** — gap per side between tray and box:\n"
                + f"```\n{rows}\n```\n"
                "Reply a preset name (`standard` = 0.15 mm), a number in mm, "
                "or `-` for standard.")

    def _pick_clearance(self, text: str) -> str:
        low = text.lower()
        if text == "-" or low == "standard":
            self.clearance = cubby.CLEARANCES["standard"]
        elif low in cubby.CLEARANCES:
            self.clearance = cubby.CLEARANCES[low]
        else:
            try:
                v = float(text)
            except ValueError:
                return self._clearance_prompt("❌ A preset name or a number, please.")
            if not 0 <= v <= 1.0:
                return self._clearance_prompt("❌ Clearance must be 0–1.0 mm per side.")
            self.clearance = v
        self.state = "height"
        return (f"✅ Clearance: **{self.clearance:.2f} mm** per side.\n\n"
                + self._height_prompt())

    def _height_prompt(self, note: str = "") -> str:
        _, _, h = self._interior()
        return ((note + "\n\n" if note else "")
                + f"How **tall** should the tray be? Interior depth is {h:g} mm.\n"
                f"Reply a number in mm, or `-` for the full {h:g} mm.")

    def _pick_height(self, text: str) -> str:
        _, _, h = self._interior()
        if text == "-":
            self.height = h
        else:
            try:
                v = float(text)
            except ValueError:
                return self._height_prompt("❌ Enter a number in mm, or `-`.")
            if not 3 <= v <= h:
                return self._height_prompt(f"❌ Between 3 and {h:g} mm.")
            self.height = v
        self.state = "confirm"
        return self._summary()

    def _summary(self) -> str:
        w, l, _ = self._interior()
        cw, cl = cubby.cell_size(self.cols, self.rows, w, l)
        return (
            f"🧩 **Insert for `{self.name}`** — ready to build\n\n"
            f"**Layout:** {self.cols} × {self.rows} "
            f"({self.cols * self.rows} compartments, ~{cw:.0f} × {cl:.0f} mm each)\n"
            f"**Clearance:** {self.clearance:.2f} mm per side\n"
            f"**Tray height:** {self.height:g} mm\n"
            f"**Output:** `{self.box_dir / 'insert.stl'}`\n\n"
            "Reply **go** to build, **edit** to start over, or **cancel**."
        )

    async def _execute(self):
        name = self.name
        box_dir = self.box_dir
        w, l, _ = self._interior()
        cols, rows, clearance, height = self.cols, self.rows, self.clearance, self.height
        corner = float(self.meta.get("insert", {}).get("corner_radius", 0.0))
        self.reset()

        self.log.info(f"inserts build starting: {name} {cols}x{rows}")
        out = box_dir / "insert.stl"
        ok, err = await asyncio.to_thread(
            cubby.build_equal, w, l, height, cols, rows, out, clearance, corner)
        return await asyncio.to_thread(
            self._report, name, box_dir, out, cols, rows, ok, err)

    def _report(self, name, box_dir, out, cols, rows, ok, err):
        if not ok:
            return f"❌ Insert for **{name}** failed to build:\n```\n{err}\n```"
        shots, warns = render_tray(out, box_dir)
        self.log.info(f"inserts build finished: {name} shots={len(shots)}")
        lines = [
            f"✅ **{name}** — cubby insert built",
            "",
            f"**Layout:** {cols} × {rows} ({cols * rows} compartments)",
            "**Print:** 0.2 mm layers, no supports (Prusa Core One, 0.4 mm nozzle)",
            "",
            f"📁 `{out}`",
        ]
        for w in warns:
            lines.append(f"⚠️ {w}")
        return "\n".join(lines), shots
