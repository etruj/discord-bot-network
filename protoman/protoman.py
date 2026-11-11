#!/usr/bin/env python3
"""Protoman: a Discord bot that turns a sketch or photo into a printable part.

The engine (tracing, sizing, OpenSCAD, the labelled drawing, revisions) lives
in engine/protobot and is reached only through engine.py. cards.py turns its
menus into Discord components; parts.py owns the folders. This file is config,
Discord plumbing and routing. README.md describes the whole design.

    python3 protoman.py --setup     configure token / channel / output folder
    python3 protoman.py --check     check dependencies and configuration
    python3 protoman.py --invite    print the link that adds the bot to a server
    python3 protoman.py             run the bot
"""
import argparse
import asyncio
import base64
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import discord
from discord import app_commands

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cards
import engine
import parts

CONFIG_PATH = Path(os.environ.get("PROTOMAN_CONFIG") or Path.home() / ".protoman.json")
DEFAULT_CONFIG = {
    "discord_token": "",
    "discord_channel_id": "",
    # Where finished parts land, one folder per version.
    "parts_root": str(Path.home() / "protoman-parts"),
    "log_file": str(Path.home() / ".protoman.log"),
    # The engine folder. "" = ./engine.
    "engine_dir": "",
}
# Environment variables win over the config file (handy for Docker).
ENV_KEYS = {
    "DISCORD_TOKEN": "discord_token",
    "PROTOMAN_CHANNEL_ID": "discord_channel_id",
    "PROTOMAN_PARTS_DIR": "parts_root",
    "PROTOMAN_LOG_FILE": "log_file",
}
# View Channel, Send Messages, Embed Links, Attach Files, Read Message History,
# Use Application Commands.
INVITE_PERMISSIONS = 2147601408
STATE_DIR = Path.home() / ".cache" / "protoman"


# ── config / logging ──────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    for env, key in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def save_config(cfg: dict):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)
    CONFIG_PATH.chmod(0o600)        # the file holds a bot token


def setup_logging(log_file: str) -> logging.Logger:
    log = logging.getLogger("protoman")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000,
                                              backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


def smart_chunks(text: str, limit: int = 1900) -> list:
    """Split into Discord-safe messages, preferring line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks, rest = [], text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks


def not_ready_message(cfg: dict) -> str:
    """What /sketch and /photo say when the engine snapshot is missing."""
    return ("🚧 Protoman's part builder isn't available.\n"
            f"Engine: {engine.status(cfg)}\n"
            "Check that the `engine/` folder is intact (see README.md).")


EDIT_RE = re.compile(r"^\s*(undo|[ECH]\d+|all|top|bottom)\b", re.I)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_UPLOAD = 25 * 1024 * 1024
HINT = "✏️ Tap a dropdown to edit, or reply to this card: `E3 up 2, H1 circle 6`"


def shrink_image(src: str, dst: str, longest: int = 1600) -> str:
    """A phone-friendly JPEG copy (trace.png is ~2 MB at full size)."""
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((longest, longest))
        im.save(dst, "JPEG", quality=85)
    return dst


def card_files(result) -> list:
    """Images first (Discord shows them as a gallery), then the printables."""
    job = Path(result.job_dir)
    paths = [result.drawing, result.preview]
    if result.version == 1 and result.overlay and Path(result.overlay).is_file():
        small = job / "trace_small.jpg"
        try:
            paths.append(str(small) if small.is_file() else shrink_image(result.overlay, str(small)))
        except Exception:  # noqa: BLE001 (a missing preview is not a failure)
            pass
    paths += [result.stl, result.scad]
    return [p for p in paths if p and Path(p).is_file()]


def card_text(key: str, result) -> str:
    name = parts.pretty(key).split(" (")[0]
    return (f"🧩 **{name}** · v{result.version}\n{result.summary()}\n"
            f"📁 `{str(result.job_dir).replace(str(Path.home()), '~', 1)}`\n{HINT}")


def modal_values(data: dict) -> dict:
    """{custom_id: value} from a modal submit, whatever the nesting
    (action rows, or the newer label components)."""
    out = {}

    def walk(node):
        if isinstance(node, dict):
            if "custom_id" in node and "value" in node:
                out[node["custom_id"]] = node["value"]
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data.get("components", []))
    return out


class PartDesk:
    """Everything between Discord events and the engine: one build at a time,
    cards, conflicts, and the half-filled forms of each user."""

    def __init__(self, cfg: dict, logger: logging.Logger, client=None):
        self.cfg, self.log, self.client = cfg, logger, client
        self.root = Path(cfg["parts_root"]).expanduser()
        self.lock = asyncio.Lock()
        self.pending = {}     # key -> {"edit": str, "acts": [...]} after a conflict
        self.forms = {}       # (user_id, key, action_id) -> {field: value}
        self.cards = {}       # key -> last card Message, to retire its buttons
        self.last = parts.LastPart(STATE_DIR / "last_part.json")

    # ── engine lookups ────────────────────────────────────────────────────
    def latest(self, key: str):
        d = parts.latest(self.root, key)
        if d is None:
            raise ValueError("I can't find that part any more "
                             f"(`{key}` under `{self.root}`).")
        return d, engine.load_result(self.cfg, str(d))

    def newest(self, key: str) -> str:
        """Folder of the newest version, looked up when a build *runs* (in
        the lock), not when it was tapped: a second tap queued behind a
        build acts on that build's result, as the buttons promise."""
        return str(self.latest(key)[0])

    def find_action(self, result, action_id: str):
        features, part_actions = engine.menus(self.cfg, result)
        for a in part_actions:
            if a["id"] == action_id:
                return a
        for f in features:
            for a in f["actions"]:
                if a["id"] == action_id:
                    return a
        raise ValueError("That button is from an older layout; use the newest card.")

    # ── posting ───────────────────────────────────────────────────────────
    async def post_card(self, send, key: str, result):
        """Send a part card with `send` (channel.send or a followup)."""
        features, part_actions = engine.menus(self.cfg, result)
        text = card_text(key, result)
        chunks = smart_chunks(text)
        for c in chunks[:-1]:
            await send(c)
        files = [discord.File(p) for p in card_files(result)]
        msg = await send(chunks[-1], files=files,
                         view=cards.card_view(key, features, part_actions))
        old = self.cards.get(key)
        if old is not None and msg is not None and old.id != getattr(msg, "id", None):
            try:
                await old.edit(view=None)
            except Exception:  # noqa: BLE001 (deleted, or too old to edit)
                pass
        if msg is not None:
            self.cards[key] = msg
        return msg

    async def build(self, channel, user_id, key: str, what: str, fn, send=None):
        """Run `fn` (sync, returns a Result) in a thread, one at a time, and
        post the outcome. `send` defaults to channel.send."""
        send = send or channel.send
        queued = self.lock.locked()
        status = await channel.send(("⏳ Waiting for the build ahead of this one… "
                                     if queued else "⏳ ") + what)
        async with self.lock:
            if queued:
                await status.edit(content="⏳ " + what)
            try:
                result = await asyncio.to_thread(fn)
            except Exception as e:  # noqa: BLE001 (reported to the user below)
                await self._report(channel, key, e, status)
                return None
        if result is None:                       # Cancel: nothing changed
            await status.delete()
            return None
        await status.delete()
        await self.post_card(send, key, result)
        self.last.set(user_id, key)
        self.log.info(f"Built {key} v{result.version} ({what})")
        return result

    async def _report(self, channel, key, err, status):
        if engine.is_conflict(self.cfg, err):
            acts = engine.conflict_menu(self.cfg, err)
            self.pending[key] = {"edit": getattr(err, "_edit", ""), "acts": acts}
            hint = ("Cancel keeps the current version; Override drops the earlier "
                    "requirement." if len(acts) > 1 else "")
            await status.edit(content=f"⚠️ **Can't do that as asked.**\n{err}\n{hint}",
                              view=cards.conflict_view(key, acts))
            return
        if isinstance(err, ValueError):
            await status.edit(content=f"❌ {err}")
            return
        self.log.exception(f"Build failed for {key}", exc_info=err)
        await status.edit(content=f"❌ Build failed: {str(err).splitlines()[0][:300]}\n"
                                  f"The job folder is under `{self.root}`.")

    def with_edit(self, fn, edit: str):
        """Wrap a build so a ConflictError remembers the edit that caused it
        (Override re-runs that edit with the blocker dropped)."""
        def run():
            try:
                return fn()
            except ValueError as e:
                e._edit = edit
                raise
        return run

    # ── typed edits ───────────────────────────────────────────────────────
    async def typed_edit(self, message, key: str, text: str):
        self.latest(key)                 # fail early if the part is gone
        await self.build(message.channel, message.author.id, key,
                         f"Revising **{parts.pretty(key)}**: `{text[:80]}`",
                         self.with_edit(lambda: engine.revise(self.cfg, self.newest(key), text), text))

    # ── components ────────────────────────────────────────────────────────
    async def on_component(self, interaction, verb: str, key: str, args: list):
        uid = interaction.user.id
        if verb == "f":                                    # a feature picked
            label = interaction.data.get("values", [""])[0]
            _, r = self.latest(key)
            features, _ = engine.menus(self.cfg, r)
            f = next((x for x in features if x["label"] == label), None)
            if f is None:
                await interaction.response.send_message(
                    f"{label} isn't on the newest version.", ephemeral=True)
                return
            await interaction.response.send_message(
                cards.feature_text(f), view=cards.feature_view(key, f), ephemeral=True)
            return

        if verb == "t":                                    # 🧰 Hole tools
            _, r = self.latest(key)
            _, part_actions = engine.menus(self.cfg, r)
            await interaction.response.send_message(
                "🧰 **Hole tools**: pick one.", view=cards.tools_view(key, part_actions),
                ephemeral=True)
            return

        if verb == "a":                                    # an action button
            _, r = self.latest(key)
            action = self.find_action(r, args[0])
            if "edit" in action or not action.get("fields"):
                await self._ack(interaction, f"⏳ {action['text']}…")
                await self.run_action(interaction, key, action, {})
                return
            if not cards.choice_fields(action):            # numbers only
                await interaction.response.send_modal(cards.number_modal(key, action, {}))
                return
            self.forms[(uid, key, action["id"])] = {}
            await self._panel(interaction, cards.collector_text(action, {}),
                              cards.collector_view(key, action, {}))
            return

        if verb == "c":                                    # a dropdown in a form
            action_id, field = args[0], args[1]
            _, r = self.latest(key)
            action = self.find_action(r, action_id)
            vals = self.forms.setdefault((uid, key, action_id), {})
            picked = interaction.data.get("values", [])
            f = next(x for x in action["fields"] if x["name"] == field)
            vals[field] = picked if f["type"] == "multi" else (picked[0] if picked else "")
            if cards.instant_choice(action):
                await self._ack(interaction, f"⏳ {action['text'].rstrip('…')}: "
                                             f"{vals[field]}…")
                await self.run_action(interaction, key, action, vals)
                return
            await interaction.response.edit_message(
                content=cards.collector_text(action, vals),
                view=cards.collector_view(key, action, vals))
            return

        if verb == "n":                                    # Next / Apply
            _, r = self.latest(key)
            action = self.find_action(r, args[0])
            vals = self.forms.get((uid, key, action["id"]), {})
            needs_numbers = cards.number_fields(action, vals) and not (
                action["id"] == "finish" and vals.get("kind") == "sharp")
            if needs_numbers:
                await interaction.response.send_modal(cards.number_modal(key, action, vals))
                return
            if action["id"] == "finish":
                vals.setdefault("mm", 0)
            await self._ack(interaction, f"⏳ {action['text'].rstrip('…')}…")
            await self.run_action(interaction, key, action, vals)
            return

        if verb == "o":                                    # conflict: cancel/override
            p = self.pending.get(key)
            i = int(args[0]) if args and args[0].isdigit() else -1
            if not p or not (0 <= i < len(p["acts"])):
                await interaction.response.edit_message(
                    content="This choice expired (the bot restarted). Make the "
                            "edit again.", view=None)
                return
            act = p["acts"][i]
            if act["do"] == "cancel":
                self.pending.pop(key, None)
                await interaction.response.edit_message(
                    content="✖️ Cancelled; the current version stays.", view=None)
                return
            await interaction.response.edit_message(
                content=f"⚠️ {act['text']}: rebuilding…", view=None)
            self.pending.pop(key, None)
            self.latest(key)
            await self.build(interaction.channel, uid, key,
                             f"{act['text']} and apply `{p['edit']}`",
                             lambda: engine.run_action(self.cfg, self.newest(key), act,
                                                       pending=p["edit"]),
                             send=self.followup_send(interaction))
            return

        if verb == "d":                                    # Done
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(
                "✅ Closed. `/part` brings any part back with its buttons.",
                ephemeral=True)
            self.cards.pop(key, None)
            return

    async def on_modal(self, interaction, key: str, args: list):
        uid = interaction.user.id
        _, r = self.latest(key)
        action = self.find_action(r, args[0])
        vals = dict(self.forms.get((uid, key, action["id"]), {}))
        raw = modal_values(interaction.data)
        try:
            for f in cards.number_fields(action, vals):
                if f["name"] in raw:
                    vals[f["name"]] = cards.parse_number(raw[f["name"]])
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}. Try again.", ephemeral=True)
            return
        await self._ack(interaction, f"⏳ {action['text'].rstrip('…')}…")
        await self.run_action(interaction, key, action, vals)

    async def run_action(self, interaction, key, action, vals):
        self.forms.pop((interaction.user.id, key, action["id"]), None)
        self.latest(key)
        try:
            edit = engine.fill(self.cfg, action, vals) if action["do"] == "edit" else ""
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        what = f"**{parts.pretty(key)}**: " + (f"`{edit}`" if edit else action["text"].rstrip("…"))

        def run():
            # Re-read the action from the newest version: a toggle carries
            # its state ("current"), which a build queued ahead may change.
            d = self.newest(key)
            act = self.find_action(engine.load_result(self.cfg, d), action["id"])
            # No values = plain button; the engine reads {} as "straighten
            # off", so a card tap could never turn it back on.
            return engine.run_action(self.cfg, d, act, vals or None)
        await self.build(interaction.channel, interaction.user.id, key, what,
                         self.with_edit(run, edit), send=self.followup_send(interaction))

    def followup_send(self, interaction):
        """Post a card as a public follow-up of the tap, like /sketch's v1.
        Images on cards sent with a plain channel.send stayed as blurred
        placeholders on a phone client; follow-ups load. Falls
        back to channel.send if the interaction token has expired (15 min,
        e.g. a long queue)."""
        async def send(content, **kw):
            try:
                return await interaction.followup.send(content, wait=True, **kw)
            except discord.HTTPException as e:
                self.log.warning(f"follow-up failed ({e}); posting to the channel")
                if kw.get("files"):              # the failed send used them up
                    kw["files"] = [discord.File(f.fp.name, filename=f.filename)
                                   for f in kw["files"]]
                return await interaction.channel.send(content, **kw)
        return send

    async def _ack(self, interaction, text: str):
        """Acknowledge a tap. A private panel shows what's happening; a tap
        on the public card just acknowledges (the status line says the rest)."""
        msg = interaction.message
        if msg is not None and msg.flags.ephemeral:
            await interaction.response.edit_message(content=text, view=None)
        else:
            await interaction.response.defer()

    async def _panel(self, interaction, text, view):
        msg = interaction.message
        if msg is not None and msg.flags.ephemeral:
            await interaction.response.edit_message(content=text, view=view)
        else:
            await interaction.response.send_message(text, view=view, ephemeral=True)


# ── Discord ───────────────────────────────────────────────────────────────
SYMMETRY = [app_commands.Choice(name="None", value="off"),
            app_commands.Choice(name="Left ↔ right", value="lr"),
            app_commands.Choice(name="Top ↔ bottom", value="tb"),
            app_commands.Choice(name="Both", value="both")]
MM = app_commands.Range[float, 0.2, 1000.0]


def check_upload(image) -> str:
    """'' if the attachment is usable, else the reason."""
    suffix = Path(image.filename).suffix.lower()
    if suffix in (".heic", ".heif"):
        return ("That's a HEIC photo. In the Discord upload, pick it from the "
                "gallery (Discord converts it), or send a JPG/PNG.")
    if not ((image.content_type or "").startswith("image/") or suffix in IMAGE_SUFFIXES):
        return "That attachment isn't an image (JPG or PNG)."
    if image.size > MAX_UPLOAD:
        return f"That image is {image.size / 2**20:.0f} MB; 25 MB is the limit."
    return ""


def build_tree(client, cfg: dict, logger: logging.Logger, desk: "PartDesk" = None):
    """Register every slash command and return the tree.

    Kept out of run_bot so tests can build the whole command surface without a
    Discord connection. discord.py validates callback signatures at decoration
    time, so a malformed handler fails here (and in the tests), not at startup.
    """
    tree = app_commands.CommandTree(client)
    channel_id = int(cfg["discord_channel_id"] or 0)
    desk = desk or PartDesk(cfg, logger, client)

    def in_channel(interaction) -> bool:
        return interaction.channel_id == channel_id

    async def refuse(interaction):
        await interaction.response.send_message(
            "Protoman only works in his own channel.", ephemeral=True)

    async def start_build(interaction, image, name, kind, run):
        """Shared start of /sketch and /photo: checks, save, build."""
        if not in_channel(interaction):
            await refuse(interaction)
            return
        try:
            engine.load(cfg)
        except ImportError:
            await interaction.response.send_message(not_ready_message(cfg))
            return
        why = check_upload(image)
        if why:
            await interaction.response.send_message(f"❌ {why}", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        job = parts.new_job_dir(desk.root, name or Path(image.filename).stem)
        suffix = Path(image.filename).suffix.lower()
        src = job / ("input" + (suffix if suffix in IMAGE_SUFFIXES else ".jpg"))
        await image.save(src)
        key = parts.family_key(job)
        logger.info(f"/{kind} by {interaction.user}: {image.filename} → {key}")

        async def send(content, **kw):
            return await interaction.followup.send(content, wait=True, **kw)

        r = await desk.build(interaction.channel, interaction.user.id, key,
                             f"Tracing and building **{parts.pretty(key)}**…",
                             lambda: run(str(src), str(job), parts.slug(name or key[16:])),
                             send=send)
        if r is None:
            # The status line above says why. Drop the empty job folder so
            # /part doesn't list a part that never existed.
            if not (job / "result.json").exists():
                shutil.rmtree(job, ignore_errors=True)
            try:
                await interaction.delete_original_response()
            except Exception:  # noqa: BLE001
                pass

    @tree.command(name="sketch",
                  description="Drawn silhouette + length/width/thickness → STL + .scad")
    @app_commands.describe(
        image="photo or scan of the drawing (dark on light, one shape)",
        length="mm, the long side (X)", width="mm (Y)",
        thickness="mm, how thick to make it (Z); 0.2 mm steps print cleanest",
        symmetry="make it exactly symmetric", straighten="clean straight edges + round holes",
        style="how it's drawn (auto works for most)", name="name for the part")
    @app_commands.choices(symmetry=SYMMETRY, style=[
        app_commands.Choice(name="Auto", value="auto"),
        app_commands.Choice(name="Filled in", value="filled"),
        app_commands.Choice(name="Outline only", value="outline")])
    async def sketch_cmd(interaction, image: discord.Attachment, length: MM,
                         width: MM, thickness: MM, symmetry: str = "off",
                         straighten: bool = False, style: str = "auto",
                         name: str = ""):
        await start_build(interaction, image, name, "sketch", lambda src, out, nm:
                          engine.build_sketch(cfg, src, out_dir=out, name=nm,
                                              length=length, width=width,
                                              thickness=thickness, mode=style,
                                              symmetry=None if symmetry == "off" else symmetry,
                                              straighten=straighten))

    @tree.command(name="photo",
                  description="Real object on paper, from above + thickness → STL + .scad")
    @app_commands.describe(
        image="the object on a sheet of paper, shot from straight above, whole sheet in frame",
        thickness="mm, how thick to make it (Z)",
        length="mm; leave empty to measure it off the paper",
        width="mm; leave empty to measure it off the paper",
        paper="the sheet under the object", symmetry="make it exactly symmetric",
        straighten="clean straight edges + round holes", name="name for the part")
    @app_commands.choices(symmetry=SYMMETRY, paper=[
        app_commands.Choice(name="Letter", value="letter"),
        app_commands.Choice(name="A4", value="a4"),
        app_commands.Choice(name="No paper (give length + width)", value="none")])
    async def photo_cmd(interaction, image: discord.Attachment, thickness: MM,
                        length: MM = None, width: MM = None, paper: str = "letter",
                        symmetry: str = "off", straighten: bool = False, name: str = ""):
        if (length is None) != (width is None):
            await interaction.response.send_message(
                "❌ Give both length and width, or neither (to measure them off "
                "the paper).", ephemeral=True)
            return
        await start_build(interaction, image, name, "photo", lambda src, out, nm:
                          engine.build_photo(cfg, src, out_dir=out, name=nm,
                                             thickness=thickness, length=length,
                                             width=width, paper=paper,
                                             symmetry=None if symmetry == "off" else symmetry,
                                             straighten=straighten))

    @tree.command(name="part", description="Bring back a part you made, with its buttons")
    @app_commands.describe(part="start typing a name; newest first")
    async def part_cmd(interaction, part: str):
        if not in_channel(interaction):
            await refuse(interaction)
            return
        try:
            _, r = desk.latest(part)
        except Exception as e:  # noqa: BLE001 (bad key, or engine missing)
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        async def send(content, **kw):
            return await interaction.followup.send(content, wait=True, **kw)
        await desk.post_card(send, part, r)
        desk.last.set(interaction.user.id, part)

    @part_cmd.autocomplete("part")
    async def _part_ac(interaction, current: str):
        cur = (current or "").lower()
        keys = [k for k in parts.families(desk.root) if cur in parts.pretty(k).lower()]
        return [app_commands.Choice(name=parts.pretty(k)[:100], value=k) for k in keys[:25]]

    @tree.command(name="protohelp", description="What Protoman can build")
    async def help_cmd(interaction):
        if not in_channel(interaction):
            await refuse(interaction)
            return
        pages = help_pages(cfg)
        await interaction.response.send_message(pages[0], ephemeral=True)
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True)

    return tree


def help_pages(cfg: dict) -> list:
    """/protohelp, as a few short private messages (each under Discord's
    2000-character limit). Keep every slash command in here: a test checks."""
    parts_dir = cfg["parts_root"].replace(str(Path.home()), "~", 1)
    return [
        "## 🤖 Protoman: sketch or photo → printable part\n"
        "Send a picture and a few measurements; get an STL to print, an "
        "OpenSCAD file, and a labelled drawing you can change by tapping.\n\n"
        "**Commands**\n"
        "**/sketch**: attach a drawing, give **length**, **width** and "
        "**thickness** (mm).\n"
        "  • Dark lines or a filled shape on light paper, one shape per photo.\n"
        "  • Options: *symmetry* (mirror it exactly), *straighten* (true "
        "straight edges, round holes), *style* (auto / filled / outline), "
        "*name*.\n"
        "**/photo**: attach a photo of the real object lying on paper, shot "
        "from straight above with the **whole sheet** in frame; give the "
        "**thickness**. Length and width are measured off the paper (or type "
        "both to override).\n"
        "  • Options: *paper* (Letter / A4 / none), *symmetry*, *straighten*, "
        "*name*.\n"
        "**/part**: bring back an earlier part with its buttons (start typing "
        "its name).\n"
        "**/protohelp**: this guide.",

        "## 🧩 The card\n"
        "Every build posts a **card**: the labelled drawing, a 3D preview, the "
        "trace (first version only, to check what was picked up), then the "
        "**.stl** and **.scad** files. ✅ means the mesh passed the print check; "
        "⚠ lines are worth a read.\n"
        "On the drawing, **E** = edges, **C** = corners, **H** = holes.\n\n"
        "**Change one feature**\n"
        "1. Tap an **✏️ Edit** dropdown (edges, corners, holes) and pick one.\n"
        "2. A private panel shows its exact size and what you can do.\n"
        "  • Plain buttons apply straight away (Straight, Sharp, Remove…).\n"
        "  • Buttons ending in **…** ask first: a dropdown for choices "
        "(direction, which hole), then a pop-up for numbers, already filled "
        "with the current value.\n"
        "  • 🎯 sets a **requirement** (e.g. E3 is exactly 20 mm) that later "
        "edits keep. 🔒 in a dropdown = locked by one.\n"
        "3. A new card arrives with the next version (v2, v3…).",

        "## 🛠️ Whole part, versions, typing\n"
        "**Card buttons**\n"
        "↩️ **Undo**: back one change.  🕘 **Versions**: return to any "
        "earlier version.  🪞 **Symmetry**: mirror left↔right, top↔bottom, "
        "both or none.  📐 **Straighten**: on/off.  ✨ **Edge finish**: round "
        "or chamfer the top/bottom edges.\n"
        "With 2+ holes: 🟰 **Same size**, ↔️ **Spacing** (centre to centre), "
        "📊 **Even spacing**.  ✅ **Done** removes the buttons (/part brings "
        "them back).\n\n"
        "**If two requirements clash** you get **Cancel** (keep the current "
        "version) or **Override** (drop the older requirement and apply the "
        "new one).\n\n"
        "**Typing instead of tapping** (optional): reply to a card, e.g. "
        "`E3 up 2, C2 R5, H1 circle 6`, `E3 is 20mm`, `top edges R1`, "
        "`H2 remove`, `undo`. A message that starts with a label goes to your "
        "last part.\n\n"
        "Only the newest card has buttons; buttons always act on the newest "
        "version. Nothing is ever deleted: every version is its own folder in "
        f"`{parts_dir}`.\n"
        f"Engine: {engine.status(cfg)}",
    ]


async def run_bot(cfg: dict, logger: logging.Logger):
    token = cfg["discord_token"]
    channel_id = int(cfg["discord_channel_id"])

    intents = discord.Intents.default()
    intents.message_content = True     # replies to a card are typed edits
    client = discord.Client(intents=intents)
    desk = PartDesk(cfg, logger, client)
    tree = build_tree(client, cfg, logger, desk)

    @client.event
    async def on_ready():
        logger.info(f"Discord bot ready, logged in as {client.user}")
        try:
            channel = await client.fetch_channel(channel_id)
            guild = discord.Object(id=channel.guild.id)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} slash commands to guild "
                        f"{channel.guild.id} (#{channel.name})")
        except Exception as e:  # noqa: BLE001 (keep running; commands may lag)
            logger.error(f"Command sync failed: {e}")

    @client.event
    async def on_interaction(interaction):
        if interaction.type not in (discord.InteractionType.component,
                                    discord.InteractionType.modal_submit):
            return
        parsed = cards.parse_cid((interaction.data or {}).get("custom_id", ""))
        if not parsed:
            return
        verb, key, args = parsed
        if interaction.channel_id != channel_id:
            await interaction.response.send_message(
                "Protoman only works in his own channel.", ephemeral=True)
            return
        try:
            if interaction.type == discord.InteractionType.modal_submit:
                await desk.on_modal(interaction, key, args)
            else:
                await desk.on_component(interaction, verb, key, args)
        except Exception as e:  # noqa: BLE001 (tell the user, keep the bot up)
            if not isinstance(e, ValueError):
                logger.exception(f"Interaction {verb} on {key} failed")
            text = f"❌ {e}" if isinstance(e, ValueError) else f"❌ That didn't work: {e}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(text[:1900], ephemeral=True)
                else:
                    await interaction.response.send_message(text[:1900], ephemeral=True)
            except Exception:  # noqa: BLE001
                pass

    @client.event
    async def on_message(message):
        """Typed edits: a reply to a card, or an edit-looking message that
        goes to the user's last part."""
        if message.author.bot or message.channel.id != channel_id:
            return
        text = message.content.strip()
        if not text:
            return
        key = ""
        ref = message.reference
        if ref and ref.message_id:
            target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if target is None:
                try:
                    target = await message.channel.fetch_message(ref.message_id)
                except Exception:  # noqa: BLE001
                    target = None
            if target is not None and target.author == client.user:
                key = card_key(target)
        if not key and EDIT_RE.match(text):
            key = desk.last.get(message.author.id)
        if not key:
            return
        try:
            await desk.typed_edit(message, key, text)
        except Exception as e:  # noqa: BLE001
            logger.exception("Typed edit failed")
            await message.reply(f"❌ {e}", mention_author=False)

    await client.start(token)


def card_key(message) -> str:
    """The part a bot message is about, from its components' custom_ids."""
    for row in getattr(message, "components", []) or []:
        for c in getattr(row, "children", []) or []:
            parsed = cards.parse_cid(getattr(c, "custom_id", "") or "")
            if parsed:
                return parsed[1]
    m = re.search(r"`[^`]*/(\d{8}_\d{6}_[a-z0-9_]+?)(?:_v\d+)?`", message.content or "")
    return m.group(1) if m else ""


# ── setup ─────────────────────────────────────────────────────────────────
def invite_url(token: str) -> str:
    """The OAuth link that adds the bot to a server. A bot token's first
    segment is the application ID, base64-encoded."""
    first = token.split(".")[0]
    app_id = base64.b64decode(first + "=" * (-len(first) % 4)).decode()
    return ("https://discord.com/oauth2/authorize?client_id=" + app_id +
            f"&permissions={INVITE_PERMISSIONS}&scope=bot+applications.commands")


def run_setup():
    cfg = load_config()
    print("=== Protoman setup ===\n")
    print("Create a bot application at https://discord.com/developers/applications\n")
    print("  • Bot → Reset Token → copy it")
    print("  • Bot → Privileged Gateway Intents → enable MESSAGE CONTENT INTENT\n")
    tok = input(f"Bot token [{'set' if cfg['discord_token'] else 'unset'}]: ").strip()
    if tok:
        cfg["discord_token"] = tok
    ch = input(f"Channel ID [{cfg['discord_channel_id'] or 'unset'}]: ").strip()
    if ch:
        cfg["discord_channel_id"] = ch
    root = input(f"Parts output folder [{cfg['parts_root']}]: ").strip()
    if root:
        cfg["parts_root"] = root
    if not cfg["discord_token"] or not cfg["discord_channel_id"]:
        print("\n⚠️  Token and channel ID are both required before Protoman runs.")
    Path(cfg["parts_root"]).expanduser().mkdir(parents=True, exist_ok=True)
    save_config(cfg)
    print(f"\n✅ Saved to {CONFIG_PATH} (mode 600)")
    print(f"✅ Parts folder: {cfg['parts_root']}")
    if cfg["discord_token"]:
        try:
            print(f"\nAdd the bot to your server:\n  {invite_url(cfg['discord_token'])}")
        except ValueError:
            print("\n⚠️  That token doesn't look right: check it was copied whole.")
    print("\nThen check everything:  python3 protoman.py --check")
    print("And start it:           python3 protoman.py")


def run_check(cfg: dict) -> bool:
    """Print a pass/fail line per requirement; True if the bot can run."""
    ok = True

    def line(passed: bool, text: str, required: bool = True):
        nonlocal ok
        ok = ok and (passed or not required)
        print(("✅" if passed else "❌" if required else "⚠️ ") + " " + text)

    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]} (3.10+)")
    status = engine.status(cfg)
    line(status.startswith("ready"), f"engine: {status}")
    scad = os.environ.get("PROTOBOT_OPENSCAD", "openscad")
    if shutil.which(scad):
        # The real test: older OpenSCAD releases have no manifold backend.
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "t.scad", Path(d) / "t.stl"
            src.write_text("cube(1);")
            cmd = [scad, "--backend=manifold", "--export-format", "binstl",
                   "-o", str(out), str(src)]
            try:
                run = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                good = run.returncode == 0 and out.is_file()
            except (OSError, subprocess.TimeoutExpired):
                good = False
        line(good, "OpenSCAD with the manifold backend" if good else
             "OpenSCAD can't export with --backend=manifold; it needs a 2025 or "
             "newer build (scripts/install_openscad.sh)")
    else:
        line(False, "OpenSCAD not found (scripts/install_openscad.sh, or set "
                    "PROTOBOT_OPENSCAD)")
    line(bool(shutil.which("admesh")), "admesh (extra mesh check)", required=False)
    line(bool(os.environ.get("DISPLAY") or shutil.which("xvfb-run")),
         "a display or xvfb-run (3D preview images)", required=False)
    line(bool(cfg["discord_token"]), "bot token configured")
    line(str(cfg["discord_channel_id"]).isdigit(), "channel ID configured")
    print("\nReady." if ok else "\nNot ready: fix the ❌ lines (see README.md).")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Protoman: sketch → printable part")
    ap.add_argument("--setup", action="store_true", help="configure and exit")
    ap.add_argument("--check", action="store_true", help="check dependencies and config")
    ap.add_argument("--invite", action="store_true", help="print the invite link")
    args = ap.parse_args()
    if args.setup:
        run_setup()
        return

    cfg = load_config()
    if args.check:
        sys.exit(0 if run_check(cfg) else 1)
    if args.invite:
        if not cfg["discord_token"]:
            sys.exit("No bot token configured: run python3 protoman.py --setup")
        print(invite_url(cfg["discord_token"]))
        return
    logger = setup_logging(cfg["log_file"])
    if not cfg["discord_token"] or not cfg["discord_channel_id"]:
        logger.error("Not configured: run python3 protoman.py --setup")
        sys.exit(1)
    logger.info(f"Starting Protoman... (engine: {engine.status(cfg)})")
    try:
        asyncio.run(run_bot(cfg, logger))
    except discord.LoginFailure:
        logger.error("Discord login failed: check the bot token.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
