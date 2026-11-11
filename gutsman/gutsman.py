#!/usr/bin/env python3
"""Gutsman — a Discord bot that designs 3D-printable boxes.

One slash command per box type (see box_types/), each spawning that type's own
wizard. The wizard ends with a folder of printable STLs and two renders.

Nothing else lives here: no sessions, no jobs, no project management — that's
MegaMan's job. This file is config, Discord plumbing, and routing.

The one exception is `/debug`, which opens a Claude Code session on Gutsman's
own source so he can be edited from the phone. It deliberately borrows
claude-pm's session helpers rather than reimplementing them, so the session is
created by the same code MegaMan uses to find and manage sessions — meaning
MegaMan sees and controls it exactly like any other.

    python3 gutsman.py --setup     configure token / channel / output folder
    python3 gutsman.py             run the bot
"""
import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
from pathlib import Path

import discord
from discord import app_commands

sys.path.insert(0, str(Path(__file__).resolve().parent))
import box_types
from box_types import inserts

CONFIG_PATH = Path.home() / ".boxbot.json"
DEFAULT_CONFIG = {
    "discord_token": "",
    "discord_channel_id": "",
    # Shared with MegaMan's /box on purpose: every generated box lives in one
    # place regardless of which bot made it.
    "boxes_root": str(Path.home() / "ai-projects" / "boxes"),
    "log_file": str(Path.home() / ".boxbot.log"),
    # Where claude-pm lives; /debug imports its session helpers from here.
    "claude_pm_dir": "",   # empty = ../megaman next to this folder, else ~/claude-pm
}

BOT_DIR = Path(__file__).resolve().parent
# The session MegaMan will see, named with *his* prefix: e.g. "cp-boxbot".
DEBUG_PROJECT = "boxbot"


# ── config / logging ──────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def save_config(cfg: dict):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_PATH)
    CONFIG_PATH.chmod(0o600)        # the file holds a bot token


def setup_logging(log_file: str) -> logging.Logger:
    log = logging.getLogger("boxbot")
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


# ── /debug: a Claude Code session on Gutsman's own source ─────────────────
def _megaman(cfg: dict):
    """Import claude-pm's session helpers.

    Gutsman does not manage sessions — MegaMan does. Rather than copy his tmux
    naming, memory caps and URL-polling (which would drift), /debug calls the
    same functions, so the session it creates is indistinguishable from one
    MegaMan started and shows up in his /status, /url, /stop and /output.
    """
    sibling = Path(__file__).resolve().parent.parent / "megaman"   # this repo's layout
    pm_dir = str(cfg.get("claude_pm_dir") or (sibling if sibling.is_dir() else Path.home() / "claude-pm"))
    if pm_dir not in sys.path:
        sys.path.insert(0, pm_dir)
    import claude_pm
    return claude_pm


async def debug_session(action: str, cfg: dict, logger: logging.Logger) -> str:
    """Start or stop the bot-dev session. Returns a message for Discord."""
    try:
        cpm = _megaman(cfg)
    except Exception as e:  # noqa: BLE001 — missing/renamed claude-pm
        logger.warning(f"/debug could not load claude-pm: {e}")
        return ("❌ Couldn't load claude-pm's session helpers from "
                f"`{cfg.get('claude_pm_dir')}` ({e}).\n"
                "Set `claude_pm_dir` in `~/.boxbot.json` if it moved.")

    pm_cfg = cpm.load_config()          # his config owns the naming convention
    sname = cpm.session_name(pm_cfg, DEBUG_PROJECT)

    if action == "stop":
        if not cpm.session_exists(sname):
            return "No bot-dev session running. `/debug start` to open one."
        ok = cpm.kill_session(sname)
        logger.info(f"Debug session stop: {'OK' if ok else 'FAILED'}")
        return ("🛠️ Bot-dev session stopped." if ok
                else "❌ Could not stop the bot-dev session.")

    cpm.trust_workspace(str(BOT_DIR), logger)
    ok, msg = await cpm.spawn_session(pm_cfg, DEBUG_PROJECT, path=BOT_DIR)
    if ok:
        msg += (
            f"\n🛠️ *Gutsman's own code (`{BOT_DIR}`). Read `LOG.md` first, and "
            "`systemctl --user restart boxbot` after changes. `/debug stop` "
            f"when done — MegaMan sees this as `{sname}` too.*"
        )
        logger.info(f"Opened bot-dev session ({sname})")
    else:
        logger.warning(f"Bot-dev session failed: "
                       f"{msg.splitlines()[0] if msg else '(no msg)'}")
    return msg


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


# ── Discord ───────────────────────────────────────────────────────────────
def build_tree(client, cfg: dict, logger: logging.Logger, wizards: dict):
    """Register every slash command and return the tree.

    Kept out of run_bot so tests can build the whole command surface without a
    Discord connection. discord.py validates callback signatures at decoration
    time, so a malformed handler blows up here — which is exactly how a bad
    parameter once got as far as a failed service start.
    """
    tree = app_commands.CommandTree(client)
    channel_id = int(cfg["discord_channel_id"])

    def in_channel(interaction) -> bool:
        return interaction.channel_id == channel_id

    async def refuse(interaction):
        await interaction.response.send_message(
            "Gutsman only works in his own channel.", ephemeral=True)

    def register(spec):
        """One command per box type; each spawns that type's own wizard.

        `spec` is captured from this function's scope, NOT bound as a default
        argument — discord.py reads the callback signature to derive the
        command's options and rejects any parameter without a type annotation,
        so `async def _cmd(interaction, _spec=spec)` fails at startup with
        "parameter '_spec' is missing a type annotation". Each call to
        register() makes a fresh closure, so there's no late-binding issue.
        """
        @tree.command(name=spec.COMMAND, description=spec.DESCRIPTION)
        async def _cmd(interaction):
            if not in_channel(interaction):
                await refuse(interaction)
                return
            wiz = spec.Wizard(cfg, logger)
            wizards[interaction.user.id] = wiz
            logger.info(f"/{spec.COMMAND} started by {interaction.user}")
            await interaction.response.send_message(wiz.start())

    for spec in box_types.TYPES:
        register(spec)

    # /inserts is not a box type (it builds *into* an existing box), so it isn't
    # in box_types.TYPES and is registered by hand. Its `box` argument is a live
    # dropdown of insert-ready boxes, like MegaMan's session picker.
    @tree.command(name=inserts.COMMAND, description=inserts.DESCRIPTION)
    @app_commands.describe(box="which existing box to build a tray for")
    async def inserts_cmd(interaction, box: str):
        if not in_channel(interaction):
            await refuse(interaction)
            return
        wiz = inserts.Wizard(cfg, logger)
        msg = wiz.start(box)
        wizards[interaction.user.id] = wiz
        logger.info(f"/{inserts.COMMAND} {box!r} started by {interaction.user}")
        await interaction.response.send_message(msg)

    @inserts_cmd.autocomplete("box")
    async def _inserts_box_ac(interaction, current: str):
        cur = (current or "").lower()
        names = [n for n in inserts.choices(cfg) if cur in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    @tree.command(name="cancel", description="Cancel the active box wizard")
    async def cancel_cmd(interaction):
        if not in_channel(interaction):
            await refuse(interaction)
            return
        wiz = wizards.get(interaction.user.id)
        msg = wiz.cancel() if wiz and wiz.is_active() else "No wizard in progress."
        await interaction.response.send_message(msg)

    @tree.command(name="debug",
                  description="Start/stop a Claude Code session on Gutsman's own code")
    @app_commands.describe(action="start or stop the bot-dev session")
    @app_commands.choices(action=[
        app_commands.Choice(name="start", value="start"),
        app_commands.Choice(name="stop", value="stop"),
    ])
    async def debug_cmd(interaction, action: str):
        if not in_channel(interaction):
            await refuse(interaction)
            return
        # Spawning polls the pane for a URL for up to ~20s, well past Discord's
        # 3-second deadline, so defer before doing any of it.
        await interaction.response.defer(thinking=True)
        try:
            msg = await debug_session(action, cfg, logger)
        except Exception as e:  # noqa: BLE001 — report instead of hanging
            logger.exception("/debug failed")
            msg = f"❌ /debug failed: {e}"
        for chunk in smart_chunks(msg):
            await interaction.followup.send(chunk)

    @tree.command(name="boxhelp", description="What Gutsman can build")
    async def help_cmd(interaction):
        if not in_channel(interaction):
            await refuse(interaction)
            return
        rows = [f"/{s.COMMAND:<12} {s.TITLE}" for s in box_types.TYPES]
        rows += [f"/{inserts.COMMAND:<12} {inserts.TITLE} (for an existing box)",
                 "/cancel      Cancel the active wizard",
                 "/debug <a>   start/stop a session on Gutsman's own code",
                 "/boxhelp     This message"]
        await interaction.response.send_message(
            "**Gutsman** — the box builder\n```\n" + "\n".join(rows) + "\n```\n"
            f"Boxes land in `{cfg['boxes_root']}/<name>/`.\n"
            "Each wizard ends with the folder path and two renders.")

    return tree


async def run_bot(cfg: dict, logger: logging.Logger):
    token = cfg["discord_token"]
    channel_id = int(cfg["discord_channel_id"])

    intents = discord.Intents.default()
    intents.message_content = True         # the wizards are driven by messages
    client = discord.Client(intents=intents)
    wizards = {}                           # user_id -> active wizard
    tree = build_tree(client, cfg, logger, wizards)

    async def send_attachments(send, attach):
        """Upload the renders. They live with the box, so they aren't deleted."""
        for p in attach or []:
            try:
                await send(file=discord.File(str(p)))
            except Exception as e:  # noqa: BLE001 — report, don't die silently
                logger.warning(f"Attachment upload failed for {p}: {e}")
                await send(f"⚠️ Image upload failed for `{Path(p).name}`: {e}")

    @client.event
    async def on_ready():
        logger.info(f"Discord bot ready — logged in as {client.user}")
        try:
            channel = await client.fetch_channel(channel_id)
            guild = discord.Object(id=channel.guild.id)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} slash commands to guild "
                        f"{channel.guild.id} (#{channel.name})")
        except Exception as e:  # noqa: BLE001 — keep running; commands may lag
            logger.error(f"Command sync failed: {e}")

    @client.event
    async def on_message(message):
        # Plain messages only ever drive an active wizard.
        if message.author == client.user or message.channel.id != channel_id:
            return
        content = message.content.strip()
        if not content:
            return
        wiz = wizards.get(message.author.id)
        if not wiz or not wiz.is_active():
            return

        logger.info(f"[{wiz.COMMAND}:{message.author}] {content!r}")
        attach = None
        try:
            async with message.channel.typing():
                response = await wiz.step(content)
            # A wizard's final step hands back images alongside the text.
            if isinstance(response, tuple):
                response, attach = response
        except Exception as e:  # noqa: BLE001 — surface it, keep wizard state
            logger.exception("Wizard step failed")
            response = f"❌ Wizard step failed: {e}\nRetry, or type `cancel`."
        for chunk in smart_chunks(response):
            await message.channel.send(chunk)
        await send_attachments(message.channel.send, attach)

    await client.start(token)


# ── setup ─────────────────────────────────────────────────────────────────
def run_setup():
    cfg = load_config()
    print("=== Gutsman (BoxBot) setup ===\n")
    print("Create a SECOND bot application at")
    print("  https://discord.com/developers/applications\n")
    print("  • Bot → Privileged Gateway Intents → enable MESSAGE CONTENT INTENT")
    print("  • OAuth2 → URL Generator → scopes: bot + applications.commands")
    print("  • Bot permissions: Send Messages, Attach Files")
    print("  • Invite it to the server, then give it access to #enclosures\n")
    tok = input(f"Bot token [{'set' if cfg['discord_token'] else 'unset'}]: ").strip()
    if tok:
        cfg["discord_token"] = tok
    ch = input(f"Channel ID [{cfg['discord_channel_id'] or 'unset'}]: ").strip()
    if ch:
        cfg["discord_channel_id"] = ch
    root = input(f"Boxes output folder [{cfg['boxes_root']}]: ").strip()
    if root:
        cfg["boxes_root"] = root
    if not cfg["discord_token"] or not cfg["discord_channel_id"]:
        print("\n⚠️  Token and channel ID are both required before Gutsman runs.")
    Path(cfg["boxes_root"]).mkdir(parents=True, exist_ok=True)
    save_config(cfg)
    print(f"\n✅ Saved to {CONFIG_PATH} (mode 600)")
    print(f"✅ Boxes root: {cfg['boxes_root']}")
    print("\nStart with:  python3 gutsman.py")
    print("Or install:  systemctl --user enable --now boxbot")


def main():
    ap = argparse.ArgumentParser(description="Gutsman — Discord box builder")
    ap.add_argument("--setup", action="store_true", help="configure and exit")
    args = ap.parse_args()
    if args.setup:
        run_setup()
        return

    cfg = load_config()
    logger = setup_logging(cfg["log_file"])
    if not cfg["discord_token"] or not cfg["discord_channel_id"]:
        logger.error("Not configured — run: python3 gutsman.py --setup")
        sys.exit(1)
    logger.info("Starting Gutsman...")
    try:
        asyncio.run(run_bot(cfg, logger))
    except discord.LoginFailure:
        logger.error("Discord login failed — check the bot token.")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
