"""Discord components for a part: the result card, the per-feature panel, the
field collector and the number pop-up. Built from the engine's ui menus
(protobot.ui), which already describe every action as either ready to run or
needing fields.

Designed for a phone:
- nothing has to be typed except numbers, and those come in a pop-up form
  pre-filled with the current value (Discord shows the number pad's keyboard)
- every choice (direction, which hole, which version, symmetry) is a dropdown
- one card per version; its buttons always act on the newest version

No callbacks live here. Every component carries a custom_id
"pm|<verb>|<family key>|<arg>" and protoman.py routes them in on_interaction,
so cards keep working across bot restarts. Views are stop()ped before sending
so discord.py doesn't hold them in its in-memory view store.
"""
import discord

PREFIX = "pm"
SEP = "|"
KIND_EMOJI = {"edge": "📏", "corner": "📐", "hole": "⭕"}
KIND_TITLE = {"edge": "Edges", "corner": "Corners", "hole": "Holes"}
PART_EMOJI = {"undo": "↩️", "revert": "🕘", "symmetry": "🪞", "straighten": "📐",
              "finish": "✨", "samesize": "🟰", "apart": "↔️", "even": "📊",
              "addhole": "➕", "scale": "🔍", "tools": "🧰"}
SHORT_TEXT = {"samesize": "Same size", "apart": "Spacing", "even": "Even spacing",
              "addhole": "Add hole", "scale": "Scale", "tools": "Hole tools",
              "revert": "Versions", "finish": "Edge finish", "symmetry": "Symmetry"}


# ── custom ids ────────────────────────────────────────────────────────────
def cid(verb: str, key: str, *args) -> str:
    s = SEP.join([PREFIX, verb, key, *[str(a) for a in args]])
    if len(s) > 100:
        raise ValueError(f"custom_id too long: {s}")
    return s


def parse_cid(custom_id: str):
    """(verb, key, [args]) for one of ours, else None."""
    parts = (custom_id or "").split(SEP)
    if len(parts) < 3 or parts[0] != PREFIX:
        return None
    return parts[1], parts[2], parts[3:]


def _clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def _view() -> discord.ui.View:
    return discord.ui.View(timeout=None)


def finish(view: discord.ui.View) -> discord.ui.View:
    """Stop the view so discord.py won't keep it; on_interaction handles it."""
    view.stop()
    return view


# ── the result card ───────────────────────────────────────────────────────
def card_view(key: str, features: list, part_actions: list) -> discord.ui.View:
    """Up to 3 feature dropdowns (edges / corners / holes), the whole-part
    buttons, hole tools and Done. Discord allows 5 rows."""
    v = _view()
    row = 0
    groups = {}
    for f in features:
        groups.setdefault(f["kind"], []).append(f)
    for kind in ("edge", "corner", "hole"):
        items = groups.get(kind, [])
        for start in range(0, len(items), 25):
            if row > 2:
                break
            chunk = items[start:start + 25]
            title = KIND_TITLE[kind] + (f" {start + 1}–{start + len(chunk)}"
                                        if len(items) > 25 else "")
            opts = [discord.SelectOption(
                        label=_clip(f"{f['label']} · {f['desc']}", 100),
                        value=f["label"],
                        description=_clip(f["value"], 100) or None,
                        emoji="🔒" if f["locked"] else KIND_EMOJI.get(kind))
                    for f in chunk]
            v.add_item(discord.ui.Select(
                custom_id=cid("f", key, kind, start), row=row, options=opts,
                placeholder=f"✏️ Edit {title.lower()}: tap one"))
            row += 1
    main = [a for a in part_actions if a["id"] in
            ("undo", "revert", "symmetry", "straighten", "finish")]
    for a in main:
        label = (a["text"] if a["id"] == "straighten"
                 else SHORT_TEXT.get(a["id"], a["text"].rstrip("…")))
        v.add_item(discord.ui.Button(
            label=_clip(label, 80), emoji=PART_EMOJI.get(a["id"]), row=row,
            custom_id=cid("a", key, a["id"]),
            style=discord.ButtonStyle.secondary,
            disabled=not a.get("enabled", True)))
    row += 1
    # last row: Add hole · Hole tools (opens a panel) · Scale · Done
    ids = {a["id"] for a in part_actions}
    for aid in ("addhole", "tools", "scale"):
        if aid == "tools" and not ids & set(HOLE_TOOLS):
            continue
        if aid != "tools" and aid not in ids:
            continue
        v.add_item(discord.ui.Button(
            label=SHORT_TEXT[aid], emoji=PART_EMOJI[aid], row=row,
            custom_id=cid("t", key) if aid == "tools" else cid("a", key, aid),
            style=discord.ButtonStyle.secondary))
    v.add_item(discord.ui.Button(label="Done", emoji="✅", row=row,
                                 custom_id=cid("d", key),
                                 style=discord.ButtonStyle.success))
    return finish(v)


# ── hole tools panel ──────────────────────────────────────────────────────
HOLE_TOOLS = ("samesize", "apart", "even")


def tools_view(key: str, part_actions: list) -> discord.ui.View:
    """Private panel behind 🧰 Hole tools: same size / spacing / even spacing."""
    v = _view()
    for a in part_actions:
        if a["id"] in HOLE_TOOLS:
            v.add_item(discord.ui.Button(
                label=SHORT_TEXT[a["id"]], emoji=PART_EMOJI[a["id"]], row=0,
                style=discord.ButtonStyle.primary, custom_id=cid("a", key, a["id"])))
    return finish(v)


# ── one feature's actions ─────────────────────────────────────────────────
def feature_text(f: dict) -> str:
    lock = "\n🔒 Locked by a requirement you set." if f["locked"] else ""
    return (f"{KIND_EMOJI.get(f['kind'], '')} **{f['label']} · {f['desc']}**\n"
            f"`{f['value']}`{lock}\nPick what to do:")


def feature_view(key: str, f: dict) -> discord.ui.View:
    """A button per action. Ones that ask for something end in '…'."""
    v = _view()
    for i, a in enumerate(f["actions"][:25]):
        needs = "edit" not in a and a.get("fields")
        style = (discord.ButtonStyle.danger if a["id"].endswith(":remove")
                 else discord.ButtonStyle.primary if needs
                 else discord.ButtonStyle.secondary)
        v.add_item(discord.ui.Button(
            label=_clip(a["text"], 80), style=style, row=i // 5,
            emoji="🎯" if a.get("requirement") else None,
            custom_id=cid("a", key, a["id"])))
    return finish(v)


# ── collecting an action's fields ─────────────────────────────────────────
def visible(f: dict, values) -> bool:
    """Fields with "when": {other: [allowed]} only show once that other
    field holds an allowed value (e.g. screw style only for M sizes).
    values=None means "don't filter"."""
    if values is None:
        return True
    return all(values.get(k) in allowed for k, allowed in f.get("when", {}).items())


def choice_fields(action: dict, values: dict = None) -> list:
    return [f for f in action.get("fields", [])
            if f["type"] in ("choice", "multi") and visible(f, values)]


def number_fields(action: dict, values: dict = None) -> list:
    return [f for f in action.get("fields", [])
            if f["type"] == "number" and visible(f, values)]


def instant_choice(action: dict) -> bool:
    """One single-choice field and nothing else: run as soon as it's picked."""
    fields = action.get("fields", [])
    return len(fields) == 1 and fields[0]["type"] == "choice"


def _choice_label(f: dict, c) -> str:
    labels = f.get("labels") or {}
    return str(labels.get(c, labels.get(str(c), c)))


def collector_text(action: dict, values: dict) -> str:
    lines = [f"**{action['text'].rstrip('…')}**"]
    for f in choice_fields(action, values):
        got = values.get(f["name"])
        shown = (", ".join(got) if isinstance(got, list)
                 else _choice_label(f, got)) if got else "—"
        lines.append(f"{f['prompt']} **{shown}**")
    if not number_fields(action, values) and not instant_choice(action):
        lines.append("Then tap **Apply**.")
    elif number_fields(action, values):
        lines.append("Then tap **Next** for the numbers.")
    return "\n".join(lines)


def collector_view(key: str, action: dict, values: dict) -> discord.ui.View:
    """A dropdown per choice field (keeps what's picked), then Next/Apply."""
    v = _view()
    cf = choice_fields(action, values)
    for row, f in enumerate(cf[:4]):
        got = values.get(f["name"])
        got = set(got) if isinstance(got, list) else {str(got)} if got else set()
        choices = list(f["choices"])[-25:] if f["name"] == "version" else list(f["choices"])[:25]
        multi = f["type"] == "multi"
        lo = min(f.get("min", 1), len(choices)) if multi else 1
        hi = min(f.get("max", len(choices)), len(choices)) if multi else 1
        v.add_item(discord.ui.Select(
            custom_id=cid("c", key, action["id"], f["name"]), row=row,
            placeholder=_clip(f["prompt"], 150), min_values=lo, max_values=hi,
            options=[discord.SelectOption(label=_clip(_choice_label(f, c), 100),
                                          value=str(c), default=str(c) in got)
                     for c in choices]))
    if not instant_choice(action):
        ready = all(values.get(f["name"]) for f in cf)
        nums = number_fields(action, values)
        v.add_item(discord.ui.Button(
            label="Next" if nums else "Apply",
            emoji="➡️" if nums else "✅",
            style=discord.ButtonStyle.success, disabled=not ready,
            row=min(len(cf), 4), custom_id=cid("n", key, action["id"])))
    return finish(v)


def number_modal(key: str, action: dict, values: dict) -> discord.ui.Modal:
    """Pop-up with one box per number field, pre-filled with current values.
    The finish action skips it when 'sharp' was picked (handled by caller)."""
    m = discord.ui.Modal(title=_clip(action["text"].rstrip("…"), 45),
                         custom_id=cid("m", key, action["id"]), timeout=None)
    for f in number_fields(action, values)[:5]:
        default = values.get(f["name"], f.get("default"))
        m.add_item(discord.ui.TextInput(
            label=_clip(f["prompt"], 45), custom_id=f["name"],
            default=None if default is None else f"{default:g}" if isinstance(
                default, (int, float)) else str(default),
            placeholder="e.g. 12.5", required=True, max_length=12))
    m.stop()
    return m


def parse_number(text: str) -> float:
    """'12', '12.5', '12,5', '12 mm' → float. ValueError with a short reason."""
    t = (text or "").strip().lower().replace("mm", "").replace(",", ".").strip()
    try:
        return float(t)
    except ValueError:
        raise ValueError(f"“{text}” isn't a number") from None


# ── conflicts ─────────────────────────────────────────────────────────────
def conflict_view(key: str, acts: list) -> discord.ui.View:
    v = _view()
    for i, a in enumerate(acts[:5]):
        v.add_item(discord.ui.Button(
            label=_clip(a["text"], 80), row=0,
            style=(discord.ButtonStyle.secondary if a["do"] == "cancel"
                   else discord.ButtonStyle.danger),
            emoji="✖️" if a["do"] == "cancel" else "⚠️",
            custom_id=cid("o", key, i)))
    return finish(v)
