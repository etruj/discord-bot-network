#!/usr/bin/env python3
"""Shared plumbing for box wizards.

Deliberately small. Each box type writes its own wizard with its own question
flow — a rugged box and, say, a gridfinity tray have little in common past
"ask a number, validate it". What lives here is only the stuff that would be
identical noise in every wizard: answer parsing, value formatting, name
sanitising, and the state flag the bot checks when routing messages.

Anything resembling a conversation belongs in the box type's own module.
"""
import re
from pathlib import Path

IDLE = "idle"


def fmt(v) -> str:
    """Render a value the way a person would type it back."""
    return {True: "yes", False: "no"}.get(v, str(v))


def sanitize_name(text: str) -> str:
    """Fold free text into a safe folder name ('' if nothing usable remains)."""
    return re.sub(r"[^a-z0-9\-_]", "", text.lower().strip().replace(" ", "-"))


def parse_answer(opt: dict, text: str):
    """Parse one answer against an option spec.

    Returns (value, None) on success or (None, error_message) so the caller can
    re-ask without advancing. Option spec keys: kind, plus choices for
    "choice" and lo/hi for int/float.
    """
    t = text.strip()
    kind = opt["kind"]
    if kind == "bool":
        if t.lower() in ("y", "yes", "true", "1", "yep", "yeah"):
            return True, None
        if t.lower() in ("n", "no", "false", "0", "nope"):
            return False, None
        return None, "Reply **yes** or **no**."
    if kind == "choice":
        if t.lower() in [c.lower() for c in opt["choices"]]:
            return next(c for c in opt["choices"] if c.lower() == t.lower()), None
        return None, "Choose one of: " + ", ".join(f"`{c}`" for c in opt["choices"])
    if kind in ("int", "float"):
        try:
            v = int(t) if kind == "int" else float(t)
        except ValueError:
            return None, f"That's not a number — enter a value in {opt.get('hint', 'range')}."
        if not (opt["lo"] <= v <= opt["hi"]):
            return None, f"Out of range — must be {opt['lo']}–{opt['hi']}."
        return v, None
    if not t:
        return None, "That can't be empty."
    return t, None


def allowed_text(opt: dict) -> str:
    """The 'allowed' column for a table row."""
    if opt["kind"] == "choice":
        return " / ".join(opt["choices"])
    if opt["kind"] == "bool":
        return "yes / no"
    if opt["kind"] == "str":
        return "text"
    return f"{opt['lo']}–{opt['hi']}"


def table(title: str, header: tuple, rows: list, footer: str,
          note: str = "") -> str:
    """A monospaced picker table. `rows` are pre-formatted strings."""
    h = f"{'#':>2}  {header[0]:<22} {header[1]:<12} {header[2]}"
    return ((note + "\n\n" if note else "")
            + f"{title}\n```\n{h}\n" + "\n".join(rows) + "\n```\n" + footer)


class BaseWizard:
    """State flag + output folder. No conversation logic — that's per box type.

    Subclasses set COMMAND/TITLE and implement `start()` and `async step()`.
    """
    COMMAND = ""
    TITLE = ""

    def __init__(self, cfg: dict, logger):
        self.cfg = cfg
        self.log = logger
        self.state = IDLE
        self.name = ""

    def is_active(self) -> bool:
        return self.state != IDLE

    def boxes_root(self) -> Path:
        return Path(self.cfg["boxes_root"])

    def reset(self):
        self.state = IDLE
        self.name = ""

    def cancel(self) -> str:
        self.log.info(f"{self.COMMAND} wizard cancelled")
        self.reset()
        return f"❌ Cancelled. Type `/{self.COMMAND}` to start again."

    def name_taken(self, name: str) -> bool:
        return (self.boxes_root() / name).exists()

    async def step(self, text: str):
        raise NotImplementedError
