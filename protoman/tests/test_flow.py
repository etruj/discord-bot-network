#!/usr/bin/env python3
"""The bot's part flow, end to end, without Discord.

Real engine builds (engine/ + OpenSCAD) driven through PartDesk with
fake channel/interaction objects that record what would be sent. This is the
offline drive of the whole flow: every kind of control on a card is
exercised once (dropdown → panel → ready button, number pop-up, dropdown +
pop-up form, instant dropdown, part button, conflict → override, typed edit).

Slow (~1 min): each step is a real build. Skipped without openscad.
"""
import asyncio
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import discord

import cards
import engine
import parts
import protoman

FIX = Path(__file__).parent / "fixtures"
HAVE_OPENSCAD = shutil.which("openscad") is not None


# ── fakes ─────────────────────────────────────────────────────────────────
class FakeMessage:
    _ids = iter(range(1000, 10**6))

    def __init__(self, content="", files=None, view=None, ephemeral=False):
        self.id = next(self._ids)
        self.content, self.files, self.view = content, files or [], view
        self.flags = SimpleNamespace(ephemeral=ephemeral)
        self.deleted = False
        self.edits = []

    async def edit(self, content=None, view=discord.utils.MISSING, **kw):
        self.edits.append((content, view))
        if content is not None:
            self.content = content
        if view is not discord.utils.MISSING:
            self.view = view

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content="", files=None, view=None, **kw):
        m = FakeMessage(content, files, view)
        self.sent.append(m)
        return m

    def live(self):
        return [m for m in self.sent if not m.deleted]


class FakeResponse:
    def __init__(self, it):
        self.it, self.done, self.modal, self.calls = it, False, None, []

    def is_done(self):
        return self.done

    async def send_message(self, content="", view=None, ephemeral=False, **kw):
        self.done = True
        self.calls.append(("send", content))
        self.it.panel = FakeMessage(content, view=view, ephemeral=ephemeral)

    async def edit_message(self, content=None, view=None, **kw):
        self.done = True
        self.calls.append(("edit", content))
        if content is not None:
            self.it.message.content = content
        self.it.message.view = view

    async def defer(self, **kw):
        self.done = True
        self.calls.append(("defer", None))

    async def send_modal(self, modal):
        self.done = True
        self.calls.append(("modal", modal.title))
        self.modal = modal


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content="", **kw):
        self.sent.append(content)
        self.last = FakeMessage(content, kw.get("files"), kw.get("view"),
                                kw.get("ephemeral", False))
        return self.last


class FakeInteraction:
    def __init__(self, channel, custom_id, values=None, message=None, components=None):
        self.user = SimpleNamespace(id=42)
        self.channel = channel
        self.channel_id = 1
        self.message = message or FakeMessage()
        self.data = {"custom_id": custom_id}
        if values is not None:
            self.data["values"] = values
        if components is not None:
            self.data["components"] = components
        self.response = FakeResponse(self)
        self.followup = FakeFollowup()
        self.panel = None


def run(coro):
    return asyncio.run(coro)


def items(view):
    return list(view.children) if view else []


def ids(view):
    return [c.custom_id for c in items(view)]


# ── the drive ─────────────────────────────────────────────────────────────
@unittest.skipUnless(HAVE_OPENSCAD, "needs openscad")
class TestPartFlow(unittest.TestCase):
    """One part, revised through every kind of control, in order."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="protoman-flow-"))
        cls.cfg = {**protoman.DEFAULT_CONFIG, "discord_channel_id": "1",
                   "parts_root": str(cls.tmp / "parts")}
        cls.desk = protoman.PartDesk(cls.cfg, logging.getLogger("flow"))
        cls.desk.last = parts.LastPart(cls.tmp / "last.json")
        cls.ch = FakeChannel()
        job = parts.new_job_dir(cls.desk.root, "bracket")
        src = job / "input.jpg"
        shutil.copy(FIX / "bracket_wobbly.jpg", src)
        cls.key = parts.family_key(job)
        cls.r1 = run(cls.desk.build(
            cls.ch, 42, cls.key, "first build",
            lambda: engine.build_sketch(cls.cfg, str(src), out_dir=str(job), name="bracket",
                                        length=90, width=87, thickness=4, symmetry="lr",
                                        straighten=True)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def latest(self):
        return self.desk.latest(self.key)[1]

    def card(self):
        return self.desk.cards[self.key]

    def step(self, coro):
        before = self.latest().version
        run(coro)
        after = self.latest()
        self.assertEqual(after.version, before + 1, [m.content for m in self.ch.sent[-3:]])
        return after

    def feature(self, desc_start):
        return next(x for x in self.latest().labels if x["desc"].startswith(desc_start))

    def test_1_first_card(self):
        r, card = self.r1, self.card()
        self.assertTrue(r.ok)
        names = [Path(f.filename).name for f in card.files]
        self.assertEqual(names[0], "drawing.png")
        self.assertIn("preview.png", names)
        self.assertIn("trace_small.jpg", names)
        self.assertTrue(any(n.endswith(".stl") for n in names))
        self.assertTrue(any(n.endswith(".scad") for n in names))
        self.assertIn("v1", card.content)
        self.assertLessEqual(len(card.content), 2000)
        rows = {c.row for c in items(card.view)}
        self.assertLessEqual(max(rows), 4)
        self.assertTrue(any(i.startswith("pm|f|") for i in ids(card.view)))
        # status line removed once the card is up
        self.assertTrue(all(m.deleted for m in self.ch.sent if m.content.startswith("⏳")))

    def test_2_dropdown_then_ready_button(self):
        f = self.feature("Right side")
        it = FakeInteraction(self.ch, cards.cid("f", self.key, "edge", 0),
                             values=[f["label"]], message=self.card())
        run(self.desk.on_component(it, "f", self.key, ["edge", "0"]))
        panel = it.panel
        self.assertTrue(panel.flags.ephemeral)
        self.assertIn(f["label"], panel.content)
        straight = next(c for c in items(panel.view) if c.custom_id.endswith(":straight"))
        old_card = self.card()
        it2 = FakeInteraction(self.ch, straight.custom_id, message=panel)
        r = self.step(self.desk.on_component(it2, "a", self.key, [f"{f['label']}:straight"]))
        self.assertTrue(r.ok)
        self.assertIsNone(old_card.view, "the previous card's buttons should be retired")
        # Revision cards go out as a follow-up of the tap (like v1), not via
        # channel.send: channel.send cards' images stuck as blurred
        # placeholders on the phone.
        self.assertIs(self.card(), it2.followup.last)
        self.assertTrue(self.card().files)

    def test_3_number_popup(self):
        f = self.feature("Right side")
        aid = f"{f['label']}:length"
        it = FakeInteraction(self.ch, cards.cid("a", self.key, aid),
                             message=FakeMessage(ephemeral=True))
        run(self.desk.on_component(it, "a", self.key, [aid]))
        modal = it.response.modal
        self.assertIsNotNone(modal, it.response.calls)
        box = modal.children[0]
        self.assertTrue(box.default)                       # pre-filled with the current length
        sub = FakeInteraction(self.ch, cards.cid("m", self.key, aid),
                              components=[{"type": 1, "components": [
                                  {"type": 4, "custom_id": "mm", "value": "45 mm"}]}])
        r = self.step(self.desk.on_modal(sub, self.key, [aid]))
        self.assertIn("(45.0 long)", next(x["value"] for x in r.labels
                                          if x["label"] == f["label"]))
        self.assertIn(f["label"], r.locked)

    def test_4_dropdown_form_then_popup(self):
        hole = next(x for x in self.latest().labels if x["kind"] == "hole")
        aid = f"{hole['label']}:move"
        panel = FakeMessage(ephemeral=True)
        it = FakeInteraction(self.ch, cards.cid("a", self.key, aid), message=panel)
        run(self.desk.on_component(it, "a", self.key, [aid]))
        nxt = [c for c in items(panel.view) if c.custom_id.startswith("pm|n|")][0]
        self.assertTrue(nxt.disabled)                      # nothing picked yet
        it = FakeInteraction(self.ch, cards.cid("c", self.key, aid, "dir"),
                             values=["left"], message=panel)
        run(self.desk.on_component(it, "c", self.key, [aid, "dir"]))
        self.assertIn("left", panel.content)
        nxt = [c for c in items(panel.view) if c.custom_id.startswith("pm|n|")][0]
        self.assertFalse(nxt.disabled)
        it = FakeInteraction(self.ch, nxt.custom_id, message=panel)
        run(self.desk.on_component(it, "n", self.key, [aid]))
        self.assertIsNotNone(it.response.modal)
        sub = FakeInteraction(self.ch, cards.cid("m", self.key, aid),
                              components=[{"type": 1, "components": [
                                  {"type": 4, "custom_id": "mm", "value": "2"}]}])
        r = self.step(self.desk.on_modal(sub, self.key, [aid]))
        self.assertTrue(r.ok)

    def test_5_instant_dropdown_and_part_button(self):
        it = FakeInteraction(self.ch, cards.cid("a", self.key, "symmetry"), message=self.card())
        run(self.desk.on_component(it, "a", self.key, ["symmetry"]))
        self.assertTrue(it.panel.flags.ephemeral)          # a private form, not on the card
        it2 = FakeInteraction(self.ch, cards.cid("c", self.key, "symmetry", "symmetry"),
                              values=["off"], message=it.panel)
        r = self.step(self.desk.on_component(it2, "c", self.key, ["symmetry", "symmetry"]))
        self.assertIsNone(r.symmetry)
        it3 = FakeInteraction(self.ch, cards.cid("a", self.key, "undo"), message=self.card())
        r = self.step(self.desk.on_component(it3, "a", self.key, ["undo"]))
        self.assertEqual(r.symmetry, "lr")

    def test_6_conflict_then_override(self):
        shoulder = self.feature("Level edge")["label"]
        before = self.latest().version
        run(self.desk.typed_edit(SimpleNamespace(channel=self.ch, author=SimpleNamespace(id=42)),
                                 self.key, f"{shoulder} is 150mm"))
        self.assertEqual(self.latest().version, before)    # nothing built
        warn = self.ch.sent[-1]
        self.assertIn("Can't do that", warn.content)
        buttons = items(warn.view)
        self.assertTrue(buttons[0].label.startswith("Cancel"))
        over = buttons[1]
        it = FakeInteraction(self.ch, over.custom_id, message=warn)
        r = self.step(self.desk.on_component(it, "o", self.key, ["1"]))
        self.assertGreater(r.length, 100)
        # The engine appends this warning after writing result.json, so it's
        # on the card (built from the returned Result) but not on disk.
        self.assertIn("override", self.card().content)

    def test_7_typed_edit_and_done(self):
        r = self.step(self.desk.typed_edit(
            SimpleNamespace(channel=self.ch, author=SimpleNamespace(id=42)),
            self.key, "undo"))
        self.assertTrue(r.ok)
        self.assertEqual(self.desk.last.get(42), self.key)
        card = self.card()
        it = FakeInteraction(self.ch, cards.cid("d", self.key), message=card)
        run(self.desk.on_component(it, "d", self.key, []))
        self.assertIsNone(card.view)
        self.assertEqual(protoman.card_key(SimpleNamespace(
            components=[], content=f"📁 `{r.job_dir}`")), self.key)

    def test_8_bad_image_is_explained(self):
        job = parts.new_job_dir(self.desk.root, "blank")
        src = job / "input.png"
        shutil.copy(FIX / "blank.png", src)
        key = parts.family_key(job)
        r = run(self.desk.build(self.ch, 42, key, "blank",
                                lambda: engine.build_sketch(self.cfg, str(src), out_dir=str(job),
                                                            name="blank", length=50, width=50,
                                                            thickness=3)))
        self.assertIsNone(r)
        self.assertTrue(self.ch.sent[-1].content.startswith("❌"))


# ── pure pieces ───────────────────────────────────────────────────────────
    def test_9_second_tap_queued_behind_a_build(self):
        """Two quick taps of a toggle: the second waits for the first build
        and must act on ITS result (newest version), not the version that
        was newest when it was tapped. So straighten goes on→off→on, and
        a plain button tap can turn it back on at all."""
        job = parts.new_job_dir(self.desk.root, "toggle")
        shutil.copy(FIX / "bracket_wobbly.jpg", job / "input.jpg")
        key = parts.family_key(job)
        run(self.desk.build(self.ch, 42, key, "first build",
            lambda: engine.build_sketch(self.cfg, str(job / "input.jpg"), out_dir=str(job),
                                        name="toggle", length=90, width=87, thickness=4,
                                        straighten=True)))

        async def two_taps():
            taps = [FakeInteraction(self.ch, cards.cid("a", key, "straighten"),
                                    message=FakeMessage()) for _ in range(2)]
            await asyncio.gather(*(self.desk.on_component(t, "a", key, ["straighten"])
                                   for t in taps))
        run(two_taps())
        hist = engine.history(self.cfg, str(self.desk.latest(key)[0]))
        self.assertEqual([(h["version"], h["parent"]) for h in hist],
                         [(1, None), (2, 1), (3, 2)])
        self.assertEqual([h["note"] for h in hist[1:]], ["straighten off", "straighten on"])

    def test_10_add_screw_hole_by_taps(self):
        """➕ Add hole → M3 → screw / top → pop-up asks only X and Y (no
        diameter for an M size) → new version with an M3 screw hole."""
        job = parts.new_job_dir(self.desk.root, "screws")
        shutil.copy(FIX / "bracket_wobbly.jpg", job / "input.jpg")
        key = parts.family_key(job)
        run(self.desk.build(self.ch, 42, key, "first build",
            lambda: engine.build_sketch(self.cfg, str(job / "input.jpg"), out_dir=str(job),
                                        name="screws", length=90, width=87, thickness=4)))
        self.assertIn(cards.cid("a", key, "addhole"), ids(self.desk.cards[key].view))
        it = FakeInteraction(self.ch, cards.cid("a", key, "addhole"),
                             message=self.desk.cards[key])
        run(self.desk.on_component(it, "a", key, ["addhole"]))
        panel = it.panel
        self.assertEqual(len([c for c in items(panel.view)
                              if isinstance(c, discord.ui.Select)]), 1)   # just the size
        for field, val in (("type", "M3"), ("style", "screw"), ("face", "top")):
            t = FakeInteraction(self.ch, cards.cid("c", key, "addhole", field),
                                values=[val], message=panel)
            run(self.desk.on_component(t, "c", key, ["addhole", field]))
        self.assertEqual(len([c for c in items(panel.view)
                              if isinstance(c, discord.ui.Select)]), 3)   # + style, face
        nxt = [c for c in items(panel.view) if c.custom_id.startswith("pm|n|")][0]
        self.assertFalse(nxt.disabled)
        t = FakeInteraction(self.ch, nxt.custom_id, message=panel)
        run(self.desk.on_component(t, "n", key, ["addhole"]))
        self.assertEqual([c.custom_id for c in t.response.modal.children], ["x", "y"])
        sub = FakeInteraction(self.ch, cards.cid("m", key, "addhole"),
                              components=[{"type": 1, "components": [
                                  {"type": 4, "custom_id": "x", "value": "45"},
                                  {"type": 4, "custom_id": "y", "value": "60"}]}])
        run(self.desk.on_modal(sub, key, ["addhole"]))
        r = self.desk.latest(key)[1]
        self.assertEqual(r.version, 2, [m.content[:200] for m in self.ch.sent[-2:]])
        self.assertTrue(any(x["desc"].startswith("M3 screw hole") for x in r.labels))

    def test_11_hole_tools_panel_and_scale(self):
        """🧰 opens the hole tools privately; 🔍 Scale → percent → 150 →
        part 1.5x with the same proportions."""
        key = next(k for k in self.desk.cards if "screws" in k)
        card = self.desk.cards[key]
        self.assertIn(cards.cid("t", key), ids(card.view))
        self.assertNotIn(cards.cid("a", key, "samesize"), ids(card.view))
        it = FakeInteraction(self.ch, cards.cid("t", key), message=card)
        run(self.desk.on_component(it, "t", key, []))
        self.assertTrue(it.panel.flags.ephemeral)
        self.assertIn(cards.cid("a", key, "samesize"), ids(it.panel.view))
        before = self.desk.latest(key)[1]
        it = FakeInteraction(self.ch, cards.cid("a", key, "scale"), message=card)
        run(self.desk.on_component(it, "a", key, ["scale"]))
        panel = it.panel
        t = FakeInteraction(self.ch, cards.cid("c", key, "scale", "by"),
                            values=["percent"], message=panel)
        run(self.desk.on_component(t, "c", key, ["scale", "by"]))
        nxt = [c for c in items(panel.view) if c.custom_id.startswith("pm|n|")][0]
        t = FakeInteraction(self.ch, nxt.custom_id, message=panel)
        run(self.desk.on_component(t, "n", key, ["scale"]))
        self.assertEqual([c.custom_id for c in t.response.modal.children], ["pct"])
        sub = FakeInteraction(self.ch, cards.cid("m", key, "scale"),
                              components=[{"type": 1, "components": [
                                  {"type": 4, "custom_id": "pct", "value": "150"}]}])
        run(self.desk.on_modal(sub, key, ["scale"]))
        r = self.desk.latest(key)[1]
        self.assertEqual((r.length, r.width), (before.length * 1.5, before.width * 1.5))
        m3 = next(x for x in r.labels if x["desc"].startswith("M3 screw hole"))
        self.assertIn("Ø3.4 thru", m3["value"])              # screws don't scale


class TestPieces(unittest.TestCase):
    def test_custom_id_round_trip(self):
        c = cards.cid("c", "20261027_141500_bracket", "H1:move", "dir")
        self.assertEqual(cards.parse_cid(c),
                         ("c", "20261027_141500_bracket", ["H1:move", "dir"]))
        self.assertIsNone(cards.parse_cid("something|else"))

    def test_numbers_from_a_phone_keyboard(self):
        for text, want in [("12", 12), ("12.5", 12.5), ("12,5", 12.5), (" 7 mm", 7)]:
            self.assertEqual(cards.parse_number(text), want)
        with self.assertRaises(ValueError):
            cards.parse_number("twelve")

    def test_modal_values_any_nesting(self):
        data = {"components": [{"type": 18, "component": {"custom_id": "x", "value": "1"}},
                               {"type": 1, "components": [{"custom_id": "y", "value": "2"}]}]}
        self.assertEqual(protoman.modal_values(data), {"x": "1", "y": "2"})

    def test_slug_never_looks_like_a_version(self):
        self.assertFalse(parts.VER_RE.search(parts.slug("bracket_v2")))
        self.assertEqual(parts.slug("IMG_2034.JPG"), "img_2034_jpg")
        self.assertEqual(parts.slug("!!!"), "part")

    def test_families_and_pretty(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("20261027_141500_a", "20261027_141500_a_v2", "20261027_150000_b"):
                (root / name).mkdir()
                (root / name / "result.json").write_text("{}")
            (root / "20261027_160000_empty").mkdir()
            self.assertEqual(parts.families(root), ["20261027_150000_b", "20261027_141500_a"])
            self.assertEqual(parts.latest(root, "20261027_141500_a").name, "20261027_141500_a_v2")
            self.assertEqual(parts.pretty("20261027_141500_a"), "a (Oct 27 14:15)")
            self.assertEqual(parts.versions(root, "../etc"), {})

    def test_card_fits_discord_limits_with_many_features(self):
        feats = [{"label": f"E{i}", "kind": "edge", "desc": "Level edge " * 20,
                  "value": "Y = 1.0 " * 30, "locked": i % 2 == 0, "actions": []}
                 for i in range(1, 31)]
        feats += [{"label": f"H{i}", "kind": "hole", "desc": "Hole", "value": "Ø5",
                   "locked": False, "actions": []} for i in range(1, 4)]
        acts = [{"id": i, "text": i} for i in
                ("undo", "revert", "symmetry", "straighten", "finish", "addhole",
                 "scale", "samesize", "apart", "even")]
        v = cards.card_view("20261027_141500_bracket", feats, acts)
        rows = {}
        for c in v.children:
            rows.setdefault(c.row, []).append(c)
        self.assertLessEqual(max(rows), 4)
        for r, cs in rows.items():
            selects = [c for c in cs if isinstance(c, discord.ui.Select)]
            self.assertTrue(len(selects) == len(cs) == 1 or not selects)
            self.assertLessEqual(len(cs), 5)
        for c in v.children:
            if isinstance(c, discord.ui.Select):
                self.assertLessEqual(len(c.options), 25)
                for o in c.options:
                    self.assertLessEqual(len(o.label), 100)
                    self.assertLessEqual(len(o.description or ""), 100)

    def test_upload_checks(self):
        ok = SimpleNamespace(filename="a.jpg", content_type="image/jpeg", size=10)
        self.assertEqual(protoman.check_upload(ok), "")
        heic = SimpleNamespace(filename="a.HEIC", content_type="image/heic", size=10)
        self.assertIn("HEIC", protoman.check_upload(heic))
        pdf = SimpleNamespace(filename="a.pdf", content_type="application/pdf", size=10)
        self.assertIn("isn't an image", protoman.check_upload(pdf))
        big = SimpleNamespace(filename="a.png", content_type="image/png", size=30 * 2**20)
        self.assertIn("limit", protoman.check_upload(big))


if __name__ == "__main__":
    unittest.main()
