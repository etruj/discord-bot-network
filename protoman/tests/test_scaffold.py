#!/usr/bin/env python3
"""Fast tests that need no OpenSCAD: the command surface builds, names are
Discord-legal, configuration works, the engine bridge fails cleanly.
"""
import base64
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import discord

import engine
import protoman

CFG = {**protoman.DEFAULT_CONFIG, "discord_channel_id": "1"}


class TestCommandSurface(unittest.TestCase):
    def setUp(self):
        client = discord.Client(intents=discord.Intents.default())
        self.tree = protoman.build_tree(client, CFG, logging.getLogger("t"))
        self.cmds = {c.name: c for c in self.tree.get_commands()}

    def test_expected_commands(self):
        self.assertEqual(set(self.cmds), {"sketch", "photo", "part", "protohelp"})

    def test_names_and_descriptions_are_discord_legal(self):
        for c in self.cmds.values():
            self.assertRegex(c.name, r"^[a-z0-9_-]{1,32}$")
            self.assertLessEqual(len(c.description), 100)

    def test_sketch_needs_image_and_all_three_sizes(self):
        req = {p.name for p in self.cmds["sketch"].parameters if p.required}
        self.assertEqual(req, {"image", "length", "width", "thickness"})

    def test_photo_size_is_optional(self):
        req = {p.name for p in self.cmds["photo"].parameters if p.required}
        self.assertEqual(req, {"image", "thickness"})


class TestHelp(unittest.TestCase):
    def test_help_covers_every_command_and_fits(self):
        client = discord.Client(intents=discord.Intents.default())
        tree = protoman.build_tree(client, CFG, logging.getLogger("t"))
        pages = protoman.help_pages(CFG)
        text = "\n".join(pages)
        for c in tree.get_commands():
            self.assertIn(f"/{c.name}", text)
        for p in pages:
            self.assertLessEqual(len(p), 2000)


class TestConfig(unittest.TestCase):
    def test_environment_wins_over_defaults(self):
        env = {"DISCORD_TOKEN": "t", "PROTOMAN_CHANNEL_ID": "42",
               "PROTOMAN_PARTS_DIR": "/somewhere"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(protoman, "CONFIG_PATH", Path("/no/such/file")):
            cfg = protoman.load_config()
        self.assertEqual((cfg["discord_token"], cfg["discord_channel_id"], cfg["parts_root"]),
                         ("t", "42", "/somewhere"))

    def test_invite_url_carries_app_id_and_permissions(self):
        token = base64.b64encode(b"1234567890").decode().rstrip("=") + ".aaa.bbb"
        url = protoman.invite_url(token)
        self.assertIn("client_id=1234567890", url)
        self.assertIn("permissions=2147601408", url)
        self.assertIn("applications.commands", url)


class TestEngineBridge(unittest.TestCase):
    def test_missing_snapshot_is_not_integrated(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"engine_dir": d}
            with self.assertRaises(ImportError):
                engine.load(cfg)
            self.assertTrue(engine.status(cfg).startswith("not integrated"))

    def test_not_ready_message_points_at_readme(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("README.md", protoman.not_ready_message({"engine_dir": d}))

    def test_defaults_to_the_engine_folder(self):
        self.assertEqual(engine.engine_dir({}), Path(engine.__file__).parent / "engine")
        self.assertEqual(engine.engine_dir({"engine_dir": ""}),
                         Path(engine.__file__).parent / "engine")


class TestChunks(unittest.TestCase):
    def test_long_text_splits_under_limit(self):
        parts = protoman.smart_chunks("line\n" * 1000, 1900)
        self.assertTrue(all(len(p) <= 1900 for p in parts))


if __name__ == "__main__":
    unittest.main()
