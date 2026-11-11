#!/usr/bin/env python3
"""Unit tests for Gutsman (BoxBot).

Covers the shared answer parsing and the rugged wizard's flow. Does not build
real geometry — that takes ~7s and needs OpenSCAD; see README for the manual
end-to-end drive.
"""
import asyncio
import logging
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import box_types
import wizard_base
from box_types import rugged


class TestRegistry(unittest.TestCase):
    def test_every_type_has_the_required_interface(self):
        for t in box_types.TYPES:
            for attr in ("COMMAND", "TITLE", "DESCRIPTION", "Wizard"):
                self.assertTrue(hasattr(t, attr), f"{t.__name__} missing {attr}")

    def test_command_names_are_discord_legal(self):
        # Discord rejects uppercase, spaces, and anything over 32 chars.
        for t in box_types.TYPES:
            self.assertRegex(t.COMMAND, r"^[a-z0-9_-]{1,32}$")

    def test_descriptions_fit_discords_limit(self):
        for t in box_types.TYPES:
            self.assertLessEqual(len(t.DESCRIPTION), 100)

    def test_lookup_by_command(self):
        self.assertIs(box_types.by_command("rugged"), rugged)
        self.assertIsNone(box_types.by_command("nope"))


class TestCommandTree(unittest.TestCase):
    """Build the real command surface offline.

    discord.py validates callback signatures when @tree.command decorates them,
    so this catches malformed handlers — e.g. binding a value with a default
    argument, which it reads as an untyped command option and rejects. That bug
    shipped once and surfaced only as a failed service start.
    """

    def _tree(self):
        import discord
        import gutsman
        client = discord.Client(intents=discord.Intents.default())
        cfg = dict(gutsman.DEFAULT_CONFIG, discord_channel_id="123")
        return gutsman.build_tree(client, cfg, logging.getLogger("test"), {})

    def test_tree_builds(self):
        self.assertIsNotNone(self._tree())

    def test_every_box_type_gets_a_command(self):
        names = {c.name for c in self._tree().get_commands()}
        for t in box_types.TYPES:
            self.assertIn(t.COMMAND, names)

    def test_support_commands_present(self):
        names = {c.name for c in self._tree().get_commands()}
        self.assertLessEqual({"cancel", "debug", "boxhelp"}, names)

    def test_box_commands_take_no_options(self):
        # A stray parameter means Discord demands an argument before the
        # wizard can even start.
        box_cmds = {t.COMMAND for t in box_types.TYPES}
        for c in self._tree().get_commands():
            if c.name in box_cmds:
                self.assertEqual(len(c.parameters), 0, f"/{c.name} has options")

    def test_debug_takes_a_single_action_choice(self):
        cmd = next(c for c in self._tree().get_commands() if c.name == "debug")
        self.assertEqual([p.name for p in cmd.parameters], ["action"])
        self.assertEqual({ch.value for ch in cmd.parameters[0].choices},
                         {"start", "stop"})


class TestAnswerParsing(unittest.TestCase):
    def _opt(self, key):
        return next(o for o in rugged.STEPS + rugged.OPTIONS + rugged.ADVANCED
                    if o["key"] == key)

    def test_int_range(self):
        self.assertEqual(wizard_base.parse_answer(self._opt("Width"), "160")[0], 160)
        self.assertIsNotNone(wizard_base.parse_answer(self._opt("Width"), "999")[1])
        self.assertIsNotNone(wizard_base.parse_answer(self._opt("Width"), "wide")[1])

    def test_bool_forms(self):
        opt = self._opt("Handle")
        for yes in ("y", "yes", "YES", "true", "1"):
            self.assertIs(wizard_base.parse_answer(opt, yes)[0], True)
        for no in ("n", "no", "false", "0"):
            self.assertIs(wizard_base.parse_answer(opt, no)[0], False)
        self.assertIsNotNone(wizard_base.parse_answer(opt, "maybe")[1])

    def test_choice_is_case_insensitive_and_canonical(self):
        opt = self._opt("Lip_Seal_Type")
        self.assertEqual(wizard_base.parse_answer(opt, "SQUARE")[0], "square")
        self.assertIsNotNone(wizard_base.parse_answer(opt, "hexagon")[1])

    def test_sanitize_name(self):
        self.assertEqual(wizard_base.sanitize_name("My Tool Case"), "my-tool-case")
        self.assertEqual(wizard_base.sanitize_name("!!!"), "")


class TestNormalize(unittest.TestCase):
    def test_auto_becomes_zero_for_the_engine(self):
        out = rugged.normalize({"Latch_Count": rugged.LATCH_AUTO, "Label": False})
        self.assertEqual(out["Latch_Count"], 0)

    def test_explicit_count_passes_through(self):
        self.assertEqual(rugged.normalize({"Latch_Count": "2", "Label": False})
                         ["Latch_Count"], "2")

    def test_label_text_dropped_when_label_off(self):
        out = rugged.normalize({"Label": False, "Label_Text": "X",
                                "Latch_Count": "2"})
        self.assertNotIn("Label_Text", out)

    def test_does_not_mutate_input(self):
        src = {"Latch_Count": rugged.LATCH_AUTO, "Label": False}
        rugged.normalize(src)
        self.assertEqual(src["Latch_Count"], rugged.LATCH_AUTO)

    def test_cli_args_use_no_prefix_for_false_bools(self):
        args = rugged._cli_args({"Handle": True, "Label": False, "Width": 160})
        self.assertIn("--handle", args)
        self.assertIn("--no-label", args)
        self.assertEqual(args[args.index("--width") + 1], "160")


class TestRuggedWizardFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wiz = rugged.Wizard({"boxes_root": self.tmp},
                                 logging.getLogger("test"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *messages):
        out = None
        for m in messages:
            out = asyncio.run(self.wiz.step(m))
        return out[0] if isinstance(out, tuple) else out   # unwrap (text, [imgs])

    def test_clasps_default_to_two_and_auto_is_offered(self):
        # Clasps are no longer asked — defaulted and editable — but the choice
        # set still must not offer the engine's literal "0" (reads as "none").
        opt = next(o for o in rugged.OPTIONS if o["key"] == "Latch_Count")
        self.assertEqual(opt["default"], "2")
        self.assertIn(rugged.LATCH_AUTO, opt["choices"])
        self.assertNotIn("0", opt["choices"])

    def test_full_run_reaches_confirm(self):
        # The plain path is now three answers: width, length, base depth.
        self.wiz.start()
        out = self._run("b1", "open", "160", "100", "45")
        self.assertIn("ready to build", out)
        self.assertEqual(self.wiz.state, "confirm")

    def test_summary_shows_the_settings_it_stopped_asking_about(self):
        # Clasps/handle/label are decided for the user now, so the summary is
        # the only place they surface — it has to name them.
        self.wiz.start()
        out = self._run("b2", "open", "160", "100", "45")
        for expected in ("Clasps", "Carry handle", "Label plate", "edit"):
            self.assertIn(expected, out)

    def test_auto_clasps_never_render_as_zero(self):
        self.wiz.start()
        self._run("b2b", "open", "160", "100", "45")
        out = self._run("edit", "6 auto", "done")     # row 6 == Clasps
        self.assertEqual(self.wiz.params["Latch_Count"], rugged.LATCH_AUTO)
        self.assertIn("auto", out)
        self.assertNotIn("Clasps:** 0", out)

    def test_label_text_row_appears_when_label_enabled(self):
        # Label is no longer a question; enabling it via `edit` must reveal the
        # text row, which is gated on it.
        self.wiz.start()
        self._run("b3", "open", "160", "100", "45")
        out = self._run("edit", "9 yes")              # row 9 == Label plate
        self.assertIn("Label text", out)
        self._run("10 TOOLS")
        self.assertEqual(self.wiz.params["Label_Text"], "TOOLS")

    def test_dash_keeps_the_default(self):
        self.wiz.start()
        self._run("b4", "open", "-", "-", "-")
        self.assertEqual(self.wiz.params["Width"], rugged.DEFAULTS["Width"])

    def test_handle_and_label_are_visible_and_typeable(self):
        """They aren't asked any more, so the summary must name them AND the
        words it suggests must actually work — one reply, not edit-by-number."""
        self.wiz.start()
        out = self._run("hb", "open", "160", "100", "45")
        self.assertIn("Carry handle", out)
        self.assertIn("Label plate", out)
        self.assertIn("`handle`", out)

        out = self._run("handle")                  # bare word toggles it on
        self.assertTrue(self.wiz.params["Handle"])
        self.assertIn("Carry handle", out)
        self._run("handle")                        # and off again
        self.assertFalse(self.wiz.params["Handle"])

        self._run("label TOOLS")
        self.assertTrue(self.wiz.params["Label"])
        self.assertEqual(self.wiz.params["Label_Text"], "TOOLS")
        self._run("label off")
        self.assertFalse(self.wiz.params["Label"])
        # Still at the summary, ready to build.
        self.assertEqual(self.wiz.state, "confirm")

    def test_overlong_label_is_rejected(self):
        self.wiz.start()
        self._run("hb2", "open", "160", "100", "45")
        out = self._run("label " + "X" * 40)
        self.assertIn("characters", out)
        self.assertFalse(self.wiz.params["Label"])

    @unittest.skipUnless(rugged.CLI.exists() and shutil.which("openscad"),
                         "needs the box-builder generator + openscad")
    def test_impossible_handle_is_refused_with_the_engines_reason(self):
        """The summary invites you to add a handle, so an impossible one has to
        be caught *there* — not after a minute of rendering.

        The gate is the generator's own `--check-only`, so the reason stays
        accurate if the engine's geometry rules change.
        """
        self.wiz.start()
        # A small box: the engine can't fit a handle on it.
        self._run("hb3", "open", "60", "60", "20")
        out = self._run("handle")
        self.assertIn("cannot be built", out)
        self.assertFalse(self.wiz.params["Handle"])   # not left half-applied
        self.assertEqual(self.wiz.state, "confirm")

    @unittest.skipUnless(rugged.CLI.exists() and shutil.which("openscad"),
                         "needs the box-builder generator + openscad")
    def test_a_handle_that_fits_is_accepted(self):
        self.wiz.start()
        self._run("hb5", "open", "180", "120", "60")
        out = self._run("handle")
        self.assertTrue(self.wiz.params["Handle"])
        self.assertIn("Carry handle", out)

    def test_unknown_confirm_reply_still_gets_help(self):
        # The shortcut handler must not swallow everything else.
        self.wiz.start()
        self._run("hb4", "open", "160", "100", "45")
        out = self._run("wat")
        self.assertIn("**go**", out)

    def test_the_common_path_stays_short(self):
        """Guards the UX promise: a usable box in a handful of replies.

        The cubby path used to take about a dozen answers. If a future change
        starts asking more questions in the linear flow, this fails and the
        question belongs in OPTIONS instead.
        """
        self.wiz.start()
        replies = ["shortbox", "cubby", "40x20, 40x20, 60x30", "go", "30"]
        for m in replies:
            self._run(m)
        self.assertEqual(self.wiz.state, "confirm")
        self.assertLessEqual(len(replies), 5)

    def test_bad_answer_reasks_without_advancing(self):
        self.wiz.start()
        self._run("b5", "open")
        before = self.wiz.idx
        self._run("enormous")                # not a valid Width
        self.assertEqual(self.wiz.idx, before)

    def test_edit_changes_one_answer(self):
        self.wiz.start()
        self._run("b6", "open", "160", "100", "45")
        self._run("edit", "2 200")           # row 1 is the name, 2 is Width
        self.assertEqual(self.wiz.params["Width"], 200)
        self.assertEqual(self.wiz.params["Length"], 100)
        self.assertIn("ready to build", self._run("done"))

    def test_edit_can_rename_and_rejects_duplicates(self):
        (Path(self.tmp) / "taken").mkdir(parents=True)
        self.wiz.start()
        self._run("mine", "open", "160", "100", "45")
        self._run("edit", "1 renamed")
        self.assertEqual(self.wiz.name, "renamed")
        out = self._run("1 taken")
        self.assertIn("already exists", out)
        self.assertEqual(self.wiz.name, "renamed")

    def test_advance_sets_only_the_chosen_option(self):
        self.wiz.start()
        self._run("b7", "open", "160", "100", "45")
        self._run("advance", "1 square")
        self.assertEqual(self.wiz.params["Lip_Seal_Type"], "square")
        self.assertEqual(self.wiz.params["Wall_Thickness"],
                         rugged.DEFAULTS["Wall_Thickness"])

    def test_duplicate_box_name_rejected(self):
        (Path(self.tmp) / "exists").mkdir(parents=True)
        self.wiz.start()
        out = self._run("exists")
        self.assertIn("already exists", out)
        self.assertEqual(self.wiz.state, "name")

    def test_cancel_resets(self):
        self.wiz.start()
        self._run("b8", "cancel")
        self.assertFalse(self.wiz.is_active())

    def test_listed_compartments_derive_the_interior(self):
        # Pick the cubby insert, list compartments; the box interior + tray are
        # sized around them, and every dimension question is skipped. The whole
        # path is now name -> cubby -> list -> go -> depth -> confirm.
        self.wiz.start()
        self._run("cbx", "cubby", "60x40, 60x20")   # -> preview
        out = self._run("go", "40")                 # confirm layout, set depth
        self.assertEqual(self.wiz.mode, "sizes")
        self.assertEqual(self.wiz.params["Width"], 65)      # 60 + walls + clearance
        self.assertEqual(self.wiz.params["Length"], 66)     # 40+20+divider+...
        self.assertEqual(self.wiz.params["Bottom_Height"], 42)
        self.assertLessEqual({"Width", "Length", "Bottom_Height"},
                             self.wiz.prefilled)
        # Nothing left to ask — the depth answer lands straight on the summary.
        self.assertEqual(self.wiz.state, "confirm")
        self.assertIn("ready to build", out)
        self.assertIn("cubby", out.lower())

    def test_count_packs_into_a_compact_grid(self):
        # A repeated count packs into a compact block, not a strip. Packed
        # layouts are pocket-based, so cell_vectors stays None.
        self.wiz.start()
        out = self._run("g1", "cubby", "8, 40x20")     # preview text
        self.assertIn("grid", out)
        self._run("go")
        _, span_w, span_l = self.wiz.layout_pockets
        self.assertEqual(len(self.wiz.layout_pockets[0]), 8)
        self.assertLess(max(span_w, span_l) / min(span_w, span_l), 1.6)

    def test_explicit_grid(self):
        self.wiz.start()
        self._run("g2", "cubby", "grid 3x2 of 40x20", "go")
        col_w, row_l = self.wiz.cell_vectors
        self.assertEqual((len(col_w), len(row_l)), (3, 2))

    def test_two_axis_custom_grid(self):
        # Reply `grid` -> column widths -> row lengths -> preview -> go.
        self.wiz.start()
        self._run("g3", "cubby", "grid", "60, 40, 30", "20, 25", "go")
        col_w, row_l = self.wiz.cell_vectors
        self.assertEqual(col_w, [60, 40, 30])
        self.assertEqual(row_l, [20, 25])

    def test_layout_preview_returns_an_image(self):
        self.wiz.start()
        self._run("g4", "cubby")
        out = asyncio.run(self.wiz.step("6, 40x20"))   # preview step
        self.assertIsInstance(out, tuple)              # (text, [image])
        self.assertTrue(out[1])                        # an image was attached

    def test_ragged_layout_odd_varying(self):
        # Rows of different counts/sizes → an odd total, box sized around them.
        self.wiz.start()
        out = self._run("rg", "cubby", "40: 1; 30: 3; 20: 60, 30", "go", "35")
        self.assertEqual(self.wiz.mode, "sizes")
        self.assertIsNone(self.wiz.cell_vectors)       # not a regular grid
        pockets, span_w, span_l = self.wiz.layout_pockets
        self.assertEqual(len(pockets), 6)
        self.assertIn("cubby", out.lower())
        self.assertEqual(self.wiz.state, "confirm")

    def test_bad_compartment_list_reasks(self):
        self.wiz.start()
        out = self._run("cbx2", "cubby", "banana")
        self.assertIn("couldn't read", out)
        self.assertEqual(self.wiz.state, "layout")

    def test_insert_menu_rejects_junk(self):
        self.wiz.start()
        out = self._run("cbx3", "sideways")
        self.assertIn("cubby", out.lower())
        self.assertEqual(self.wiz.state, "insert")

    def test_missing_generator_is_reported_not_raised(self):
        res = rugged.build({"Width": 160}, Path(self.tmp) / "x")
        self.assertIn(res["status"], ("unavailable", "invalid", "ok",
                                      "render_failed", "tests_failed"))


class TestCubby(unittest.TestCase):
    def test_arrange_splits_toward_square_cells(self):
        import cubby
        # A very wide box should lay 4 out across the width, not stack them.
        self.assertEqual(cubby.arrange(4, 200, 60), (4, 1))
        # A near-square box should grid 4 as 2x2.
        self.assertEqual(cubby.arrange(4, 100, 100), (2, 2))
        # A prime just becomes a single row/column.
        self.assertIn(cubby.arrange(3, 100, 40), ((3, 1), (1, 3)))

    def test_feasible_counts_drops_layouts_with_tiny_cells(self):
        import cubby
        # Roomy box offers the full ladder.
        self.assertEqual(cubby.feasible_counts(180, 120), list(cubby.SPLIT_COUNTS))
        # A tiny box offers nothing.
        self.assertEqual(cubby.feasible_counts(25, 25), [])

    def test_cell_ok_matches_the_min_cell_rule(self):
        import cubby
        self.assertTrue(cubby.cell_ok(2, 1, 100, 40))
        self.assertFalse(cubby.cell_ok(10, 10, 40, 40))

    def test_tray_outer_grows_from_cells(self):
        import cubby
        # Two cells 40 + 20 along Y, one 60 column: + one divider + two walls.
        ow, ol = cubby.tray_outer([60], [40, 20])
        self.assertAlmostEqual(ow, 60 + 2 * cubby.WALL)
        self.assertAlmostEqual(ol, 60 + cubby.DIVIDER + 2 * cubby.WALL)

    def test_equal_cells_fit_the_interior_minus_clearance(self):
        import cubby
        cw, rl = cubby.equal_cells(100, 60, 2, 2, clearance=0.15)
        self.assertEqual(len(cw), 2)
        self.assertEqual(len(rl), 2)
        # Rebuilding the tray outer from these cells lands within the interior.
        ow, ol = cubby.tray_outer(cw, rl)
        self.assertLessEqual(ow, 100)
        self.assertLessEqual(ol, 60)

    def test_defines_passes_cells_as_vectors(self):
        import cubby
        d = cubby.defines([60], [40, 20], 41.5, corner_radius=4.85)
        self.assertEqual(d["col_widths"], [60])
        self.assertEqual(d["row_lengths"], [40, 20])
        self.assertAlmostEqual(d["corner_radius"], 4.85)


class TestSpec(unittest.TestCase):
    def test_parse_compartments(self):
        import spec
        cells, err = spec.parse_compartments("60x40, 60x20")
        self.assertIsNone(err)
        self.assertEqual(cells, [(60.0, 40.0), (60.0, 20.0)])
        # accepts the × glyph and semicolons too
        cells, err = spec.parse_compartments("60×40; 60×20")
        self.assertEqual(cells, [(60.0, 40.0), (60.0, 20.0)])

    def test_parse_rejects_junk(self):
        import spec
        cells, err = spec.parse_compartments("banana")
        self.assertIsNone(cells)
        self.assertIn("couldn't read", err)

    def test_count_shorthand_expands(self):
        import spec
        cells, err = spec.parse_compartments("7, 4x20")
        self.assertIsNone(err)
        self.assertEqual(cells, [(4.0, 20.0)] * 7)
        # counts and explicit sizes can mix
        cells, err = spec.parse_compartments("3, 50x30, 60x40")
        self.assertEqual(cells, [(50.0, 30.0)] * 3 + [(60.0, 40.0)])

    def test_dangling_count_errors(self):
        import spec
        cells, err = spec.parse_compartments("7")
        self.assertIsNone(cells)
        self.assertIn("needs a size", err)
        cells, err = spec.parse_compartments("40x20, 7")
        self.assertIsNone(cells)
        self.assertIn("needs a size", err)

    def test_a_typed_list_is_packed_not_stacked(self):
        """The reported bug: listing compartments produced a long strip.

        A list typed straight at the layout prompt used to fall through to a
        naive single-column arrangement — only replying `auto` first got the
        real packer. Now the list itself is packed, so the easy input gives the
        compact tray.
        """
        import spec
        b = spec.LayoutBuilder()
        b.feed("40x20, 40x20, 60x30, 20x20")
        # Old behaviour was 60 x 94.5 — a stack. Anything near-square beats it.
        self.assertLess(max(b.span_w, b.span_l), 80)
        self.assertLess(max(b.span_w, b.span_l) / min(b.span_w, b.span_l), 1.5)

    def test_prime_counts_are_not_forced_into_strips(self):
        """Counts with no useful factors used to have no compact grid.

        The old arranger only considered exact divisor pairs, so 5 and 7 could
        only be 1xN or Nx1. Shelf packing has no such restriction.
        """
        import spec
        for text, limit in (("7, 40x20", 100), ("5, 50x30", 110)):
            b = spec.LayoutBuilder()
            b.feed(text)
            self.assertLess(max(b.span_w, b.span_l), limit,
                            f"{text} packed into a strip: "
                            f"{b.span_w} x {b.span_l}")

    def test_auto_rotates_cells_to_pack_tighter(self):
        """Auto may turn a cell 90° — the same hole, laid down the other way."""
        import spec
        cells = [(60, 20), (60, 20), (20, 60)]
        _, w_no, l_no, _ = spec.auto_pack(cells, rotate=False)
        _, w_rot, l_rot, note = spec.auto_pack(cells, rotate=True)
        self.assertLess(w_rot * l_rot, w_no * l_no)      # strictly tighter
        self.assertIn("turned 90", note)                 # and it says so

    def test_rotation_never_makes_the_packing_worse(self):
        """Shelf packing is greedy, so rotation could send it down a worse path.

        auto_pack searches rotated and as-listed arrangements and keeps the best
        of both, which makes rotation strictly additive.
        """
        import spec
        for text in ("7, 40x20", "5, 50x30", "6 of 40x20",
                     "40x20, 40x20, 60x30, 20x20",
                     "40x20, 40x20, 60x30, 20x20, 30x30, 50x20"):
            cells, _ = spec.parse_compartments(spec.normalize_count_form(text))
            _, w0, l0, _ = spec.auto_pack(cells, rotate=False)
            _, w1, l1, _ = spec.auto_pack(cells, rotate=True)
            self.assertLessEqual(w1 * l1, w0 * l0 + 1e-6,
                                 f"rotation regressed {text}")

    def test_manual_layouts_are_never_rotated(self):
        """The other half of the deal: hand-placed cells stay exactly as drawn."""
        import spec
        # A drawn grid: one 20-wide cell over one 60-wide cell. If anything
        # rotated, these exact widths/lengths would not survive.
        b = spec.LayoutBuilder()
        b.feed("20x60\n60x20")
        placed = {(p[2], p[3]) for p in b.pockets}
        self.assertEqual(placed, {(20.0, 60.0), (60.0, 20.0)})
        # An explicit grid keeps its orientation too.
        b2 = spec.LayoutBuilder()
        b2.feed("grid 1x2 of 20x60")
        self.assertEqual({(p[2], p[3]) for p in b2.pockets}, {(20.0, 60.0)})

    def test_identical_cells_read_as_a_grid(self):
        import spec
        b = spec.LayoutBuilder()
        b.feed("6 of 40x20")
        self.assertEqual((b.span_w, b.span_l), (81.5, 63.0))   # 2 across, 3 down
        self.assertIn("grid", b.note)
        # 7 narrow cells pack across the width, not into a 140mm strip
        b2 = spec.LayoutBuilder()
        b2.feed("7, 4x20")
        self.assertLess(b2.span_l, 30)

    def test_explicit_grid_is_honoured_verbatim(self):
        import spec
        res, err = spec.parse_explicit_grid("grid 3x2 of 40x20")
        self.assertIsNone(err)
        col_w, row_l, _ = res
        self.assertEqual(col_w, [40, 40, 40])
        self.assertEqual(row_l, [20, 20])
        # Asking for a deliberate strip must still give one — an explicit grid
        # pins the arrangement, so the packer does not "improve" it.
        b = spec.LayoutBuilder()
        b.feed("grid 1x5 of 50x30")
        self.assertEqual((b.span_w, b.span_l), (50.0, 156.0))

    def test_non_grid_text_is_not_treated_as_a_grid(self):
        import spec
        res, err = spec.parse_explicit_grid("40x20, 60x30")
        self.assertIsNone(res)      # not a grid...
        self.assertIsNone(err)      # ...but not an error either — just a list

    def test_n_of_wxl_form(self):
        import spec
        cells, err = spec.parse_compartments(spec.normalize_count_form("6 of 40x20"))
        self.assertIsNone(err)
        self.assertEqual(cells, [(40.0, 20.0)] * 6)

    def test_ragged_pipe_separator(self):
        import spec
        rows, err = spec.parse_rows("40: 1 | 30: 3 | 20: 60, 30")
        self.assertIsNone(err)
        self.assertEqual(len(rows), 3)

    def test_auto_pack_arranges_a_flat_list(self):
        import spec
        cells = [(40, 20), (40, 20), (60, 30), (20, 20), (20, 20), (20, 20)]
        pockets, span_w, span_l, note = spec.auto_pack(cells)
        self.assertEqual(len(pockets), 6)             # every cell placed
        # exact sizes preserved (a listed compartment is a promise)
        self.assertIn((60, 30), [(p[2], p[3]) for p in pockets])
        self.assertIn("6 compartments", note)

    def test_auto_pack_prefers_square(self):
        import spec
        # 60x40, 40x20, 20x20 should NOT stack in one column; the 40 and 20
        # share a row → a compact ~square box, not a tall strip.
        _, w, l, note = spec.auto_pack([(60, 40), (40, 20), (20, 20)])
        self.assertLess(max(w, l) / min(w, l), 1.1)      # near-square
        self.assertIn("2 rows", note)
        # four equal cells → a 2x2 square, not a 1x4 strip
        _, w, l, _ = spec.auto_pack([(50, 50)] * 4)
        self.assertAlmostEqual(w, l)                      # perfectly square

    def test_auto_pack_does_not_waste_space_for_squareness(self):
        import cubby
        import spec
        # The screenshot bug: 40x60, 40x20, 20x20 used to arrange into a
        # perfectly-square 81.5x81.5 bounding box by parking the 20x20 in a
        # 60-tall row — squareness bought with a huge void. The packer must now
        # prefer the tighter (less-wasteful) arrangement.
        cells = [(40, 60), (40, 20), (20, 20)]
        _, w, l, _ = spec.auto_pack(cells)
        used = sum(a * b for a, b in cells)
        # Comfortably tighter than the old square (which wasted ~3040 mm²).
        self.assertLess(w * l - used, 2000)
        self.assertLessEqual(max(w, l), 81.5 + cubby.DIVIDER)  # no larger footprint

    def test_auto_mode_via_builder(self):
        import spec
        b = spec.LayoutBuilder()
        b.feed("auto")
        step = b.feed("40x20, 40x20, 60x30, 20x20")
        self.assertTrue(b.ragged)
        self.assertEqual(len(b.pockets), 4)
        self.assertTrue(step.preview)

    def test_visual_grid_rows(self):
        import spec
        # the user's example: two cells on top, one full-width below
        rows, err = spec.parse_cell_rows("20x20, 20x20\n20x40")
        self.assertIsNone(err)
        self.assertEqual(rows, [(20.0, [20.0, 20.0]), (40.0, [20.0])])
        pockets, span_w, span_l, _ = spec.row_pockets(rows)
        self.assertEqual(len(pockets), 3)
        self.assertAlmostEqual(span_w, 41.5)          # 20 + 20 + divider
        # the lone bottom cell keeps its EXACT width (not stretched) and centres
        bottom = [p for p in pockets if p[1] > 0][0]
        self.assertAlmostEqual(bottom[2], 20.0)       # exact, not 41.5
        self.assertAlmostEqual(bottom[0], (41.5 - 20) / 2)   # centred → wall slack

    def test_visual_grid_via_builder_multiline(self):
        import spec
        b = spec.LayoutBuilder()
        step = b.feed("20x20, 20x20\n20x40")
        self.assertTrue(b.ragged)
        self.assertEqual(len(b.pockets), 3)
        self.assertTrue(step.preview)

    def test_parse_rows_ragged(self):
        import spec
        rows, err = spec.parse_rows("40: 1; 30: 3; 20: 60, 30")
        self.assertIsNone(err)
        self.assertEqual(rows, [(40.0, 1), (30.0, 3), (20.0, [60.0, 30.0])])

    def test_row_pockets_odd_count_varying_sizes(self):
        import spec
        rows, _ = spec.parse_rows("40: 1; 30: 3; 20: 60, 30")
        pockets, span_w, span_l, note = spec.row_pockets(rows)
        self.assertEqual(len(pockets), 6)          # 1 + 3 + 2, an odd total
        # every row fills the same interior width
        self.assertAlmostEqual(span_w, 91.5)
        self.assertIn("rows", note)

    def test_parse_rows_rejects_bad_row(self):
        import spec
        _, err = spec.parse_rows("40: 1; nonsense")
        self.assertIsNotNone(err)

    def test_grid_and_ragged_share_pocket_form(self):
        import spec
        gp, _, _ = spec.grid_pockets([40, 40], [20, 20])
        self.assertEqual(len(gp), 4)
        self.assertEqual(len(gp[0]), 4)            # (x, y, w, l)

    def test_interior_for_the_example(self):
        import spec
        # the canonical example, as a single column of two cells
        col_w, row_l = [60], [40, 20]
        interior = spec.interior_for(col_w, row_l, 40, clearance=0.15)
        # 60 + 2*wall(2) = 64, +2*0.15 -> ceil 65
        self.assertEqual(interior["width"], 65)
        # 60 + divider(1.5) + 2*wall = 65.5, +0.3 -> ceil 66
        self.assertEqual(interior["length"], 66)
        # depth 40 + floor(1.5) + gap(0.5) -> 42
        self.assertEqual(interior["height"], 42)


class TestBoxmeta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        import boxmeta
        d = Path(self.tmp) / "b"
        meta = boxmeta.make("b", "rugged",
                            {"width": 160, "length": 100, "height": 40},
                            insert_supported=True, corner_radius=5.0)
        boxmeta.write(d, meta)
        got = boxmeta.read(d)
        self.assertEqual(got["internal"]["width"], 160)
        self.assertTrue(got["insert"]["supported"])

    def test_read_missing_is_none(self):
        import boxmeta
        self.assertIsNone(boxmeta.read(Path(self.tmp) / "nope"))

    def test_scan_filters_to_insert_ready(self):
        import boxmeta
        boxmeta.write(Path(self.tmp) / "yes", boxmeta.make(
            "yes", "rugged", {"width": 1, "length": 1, "height": 1},
            insert_supported=True))
        boxmeta.write(Path(self.tmp) / "no", boxmeta.make(
            "no", "sliding", {"width": 1, "length": 1, "height": 1},
            insert_supported=False))
        names = {m["name"] for m in boxmeta.scan(self.tmp, insert_only=True)}
        self.assertEqual(names, {"yes"})
        allnames = {m["name"] for m in boxmeta.scan(self.tmp, insert_only=False)}
        self.assertEqual(allnames, {"yes", "no"})


class TestSliding(unittest.TestCase):
    def test_interior_becomes_outer(self):
        from box_types import sliding
        p = dict(sliding.DEFAULTS, Internal_Width=120, Internal_Length=80,
                 Internal_Height=30)
        d = sliding._defines(p)
        self.assertEqual(d["box_width"], 120 + 2 * p["Wall_Thickness"])
        self.assertEqual(d["box_length"], 80 + 2 * p["Wall_Thickness"])
        # box_height carries the lid track, floor and clearances on top of the
        # usable interior depth.
        expected = (30 + sliding.GROOVE_MARGIN_TOP + p["Lid_Thickness"]
                    + p["Floor_Thickness"] + sliding.DIVIDER_CLEARANCE)
        self.assertAlmostEqual(d["box_height"], expected)

    def test_blank_lid_text_disables_engraving(self):
        from box_types import sliding
        d = sliding._defines(dict(sliding.DEFAULTS, Lid_Text=""))
        self.assertFalse(d["enable_text"])
        d2 = sliding._defines(dict(sliding.DEFAULTS, Lid_Text="HI"))
        self.assertTrue(d2["enable_text"])

    def test_even_grid_flow_reaches_confirm(self):
        from box_types import sliding
        tmp = tempfile.mkdtemp()
        try:
            wiz = sliding.Wizard({"boxes_root": tmp}, logging.getLogger("test"))
            wiz.start()
            out = None
            for m in ("sb", "even", "120", "80", "30", "2", "3"):
                out = asyncio.run(wiz.step(m))
            self.assertIn("ready to build", out)
            self.assertEqual(wiz.state, "confirm")
            self.assertEqual(wiz.mode, "open")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_custom_compartments_drive_the_interior(self):
        from box_types import sliding
        tmp = tempfile.mkdtemp()
        try:
            wiz = sliding.Wizard({"boxes_root": tmp}, logging.getLogger("test"))
            wiz.start()
            out = None
            for m in ("sc", "custom", "60x40, 60x20", "go", "30"):
                out = asyncio.run(wiz.step(m))
                if isinstance(out, tuple):
                    out = out[0]
            self.assertEqual(wiz.mode, "sizes")
            # interior = cells + divider, no tray/clearance
            self.assertEqual(wiz.params["Internal_Width"], 60)
            self.assertEqual(wiz.params["Internal_Length"], 61.5)   # 40+20+1.5
            self.assertEqual(wiz.params["Internal_Height"], 30)
            self.assertIn("ready to build", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_lid_text_fit_check(self):
        from box_types import sliding
        small = dict(sliding.DEFAULTS, Internal_Length=41.5)
        self.assertFalse(sliding.text_fits("GUTSMAN", small)[0])   # overruns
        big = dict(sliding.DEFAULTS, Internal_Length=160)
        self.assertTrue(sliding.text_fits("GUTSMAN", big)[0])      # plenty of room

    def test_lid_text_length_matches_openscad(self):
        # Regression: getbbox() undercounted by ~40%, so long labels passed the
        # fit check and overshot the lid. The estimate must track OpenSCAD's real
        # render (Liberation Sans Bold): "ABCDEFGHIJKL" @ size 6 ≈ 64.8 mm.
        from box_types import sliding
        est = sliding._text_length_mm("ABCDEFGHIJKL")
        self.assertAlmostEqual(est, 64.8, delta=2.0)
        # …and that length must be rejected on the 80 mm box it used to slip past.
        box80 = dict(sliding.DEFAULTS, Internal_Length=80)
        self.assertFalse(sliding.text_fits("ABCDEFGHIJKL", box80)[0])

    def test_wizard_rejects_overlong_lid_text(self):
        from box_types import sliding
        tmp = tempfile.mkdtemp()
        try:
            wiz = sliding.Wizard({"boxes_root": tmp}, logging.getLogger("test"))
            wiz.start()
            out = None
            # The engraving is set through `edit` now (row 7), but the fit check
            # must still catch text too long for the lid.
            for m in ("tiny", "even", "35", "35", "30", "1", "1",
                      "edit", "7 GUTSMANBOX"):
                out = asyncio.run(wiz.step(m))
                if isinstance(out, tuple):
                    out = out[0]
            self.assertIn("fit", out.lower())          # caught, not cut off
            self.assertNotEqual(wiz.params["Lid_Text"], "GUTSMANBOX")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_defines_pass_pockets_when_custom(self):
        from box_types import sliding
        import spec
        p = dict(sliding.DEFAULTS)
        pockets, sw, sl = spec.grid_pockets([60], [40, 20])   # span 60 x 61.5
        d = sliding._defines(p, layout=(pockets, sw, sl))
        self.assertEqual(len(d["pockets"]), 2)
        self.assertEqual(d["box_width"], round(sw + 2 * p["Wall_Thickness"], 3))
        self.assertEqual(d["box_length"], round(sl + 2 * p["Wall_Thickness"], 3))


class TestProject(unittest.TestCase):
    def test_interior_becomes_outer(self):
        from box_types import project
        p = dict(project.DEFAULTS, Internal_Width=100, Internal_Length=140,
                 Internal_Height=40)
        d = project._defines(p)
        self.assertEqual(d["box_width"], 100 + 2 * p["Wall_Thickness"])
        self.assertEqual(d["box_length"], 140 + 2 * p["Wall_Thickness"])
        # box_height is the body only (floor to wall top) — the lid sits flush
        # on top and contributes no cavity depth.
        self.assertAlmostEqual(d["box_height"], 40 + p["Wall_Thickness"])

    def test_hole_deepness_clamped_for_shallow_boxes(self):
        from box_types import project
        p = dict(project.DEFAULTS, Internal_Height=10)   # box_height = 13
        d = project._defines(p)
        # must leave at least MIN_BOTTOM under the pilot hole
        self.assertLessEqual(d["hole_deepness"], d["box_height"] - project.MIN_BOTTOM)

    def test_mid_span_screws_added_past_threshold(self):
        from box_types import project
        small = dict(project.DEFAULTS, Internal_Width=100, Internal_Length=100)
        d = project._defines(small)
        self.assertFalse(d["add_x_mid"])
        self.assertFalse(d["add_y_mid"])
        big = dict(project.DEFAULTS, Internal_Width=100, Internal_Length=250)
        d2 = project._defines(big)
        self.assertTrue(d2["add_x_mid"])
        self.assertFalse(d2["add_y_mid"])

    def test_screw_style_maps_to_hole_type(self):
        from box_types import project
        for style, expected in project.SCREW_STYLES.items():
            d = project._defines(dict(project.DEFAULTS, Screw_Style=style))
            self.assertEqual(d["hole_type"], expected)

    def test_plain_flow_reaches_confirm(self):
        from box_types import project
        tmp = tempfile.mkdtemp()
        try:
            wiz = project.Wizard({"boxes_root": tmp}, logging.getLogger("test"))
            wiz.start()
            out = None
            for m in ("pb", "none", "100", "140", "-"):
                out = asyncio.run(wiz.step(m))
            self.assertIn("ready to build", out)
            self.assertEqual(wiz.state, "confirm")
            self.assertEqual(wiz.mode, "open")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_custom_compartments_drive_the_interior(self):
        from box_types import project
        tmp = tempfile.mkdtemp()
        try:
            wiz = project.Wizard({"boxes_root": tmp}, logging.getLogger("test"))
            wiz.start()
            out = None
            for m in ("pc", "cubby", "60x40, 60x20", "go", "30"):
                out = asyncio.run(wiz.step(m))
                if isinstance(out, tuple):
                    out = out[0]
            self.assertEqual(wiz.mode, "sizes")
            self.assertIn("ready to build", out)
            self.assertIn("cubby", out.lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCircular(unittest.TestCase):
    def test_interior_becomes_outer(self):
        from box_types import circular
        p = dict(circular.DEFAULTS, Internal_Diameter=80, Internal_Height=40,
                 Lid_Style="lip")
        d = circular._defines(p)
        self.assertEqual(d["outer_dia"], 80 + 2 * p["Wall_Thickness"])
        # A lip lid hangs into the bore, so the body carries the floor AND the
        # lip on top of the depth the user asked for.
        self.assertAlmostEqual(
            d["body_height"],
            40 + p["Floor_Thickness"] + p["Lip_Height"])

    def test_screw_lid_costs_no_depth(self):
        from box_types import circular
        p = dict(circular.DEFAULTS, Internal_Height=40, Lid_Style="screw")
        d = circular._defines(p)
        # Flat lid on top of the wall — only the floor is added.
        self.assertAlmostEqual(d["body_height"], 40 + p["Floor_Thickness"])

    def test_hole_deepness_clamped_for_shallow_boxes(self):
        from box_types import circular
        p = dict(circular.DEFAULTS, Internal_Height=10, Lid_Style="screw")
        d = circular._defines(p)
        self.assertLessEqual(d["hole_deepness"],
                             d["body_height"] - circular.MIN_BOTTOM)

    def test_screw_style_maps_to_hole_type(self):
        from box_types import circular
        for style, expected in circular.SCREW_STYLES.items():
            d = circular._defines(dict(circular.DEFAULTS, Screw_Style=style))
            self.assertEqual(d["hole_type"], expected)

    def test_footprint_grows_only_for_screw_ears(self):
        from box_types import circular
        lip = circular._defines(dict(circular.DEFAULTS, Lid_Style="lip"))
        self.assertEqual(circular.footprint(lip), lip["outer_dia"])
        screw = circular._defines(dict(circular.DEFAULTS, Lid_Style="screw"))
        self.assertGreater(circular.footprint(screw), screw["outer_dia"])

    def test_meta_reports_the_true_diameter_and_no_insert_support(self):
        from box_types import circular
        meta = circular._meta("round", dict(circular.DEFAULTS,
                                            Internal_Diameter=93,
                                            Internal_Height=32))
        # The honest interior, not a rectangle contorted to suit /inserts.
        self.assertEqual(meta["internal"]["width"], 93)
        self.assertEqual(meta["internal"]["length"], 93)
        self.assertEqual(meta["internal"]["height"], 32)
        # /inserts sizes rectangular trays, which has no honest answer for a
        # bore — so round boxes stay out of its picker.
        self.assertFalse(meta["insert"]["supported"])

    def test_round_boxes_are_excluded_from_the_inserts_picker(self):
        import boxmeta
        from box_types import circular
        tmp = tempfile.mkdtemp()
        try:
            root = Path(tmp)
            boxmeta.write(root / "round", circular._meta(
                "round", dict(circular.DEFAULTS, Internal_Diameter=93)))
            boxmeta.write(root / "square", boxmeta.make(
                "square", "project", {"width": 90, "length": 90, "height": 40},
                insert_supported=True))
            listed = [m["name"] for m in boxmeta.scan(root)]
            self.assertIn("square", listed)
            self.assertNotIn("round", listed)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_layout_is_sized_by_its_diagonal(self):
        from box_types import circular
        # A rectangle fits a circle when its diagonal does — sizing by either
        # side alone would let the corners through the wall.
        interior = {"width": 60, "length": 80, "height": 30}
        dia = circular.diameter_for_span(interior)
        self.assertGreaterEqual(dia, math.hypot(60, 80))
        self.assertGreater(dia, max(60, 80))

    def test_plain_flow_reaches_confirm(self):
        from box_types import circular
        tmp = tempfile.mkdtemp()
        try:
            wiz = circular.Wizard({"boxes_root": tmp}, logging.getLogger("test"))
            wiz.start()
            out = None
            for m in ("cb", "none", "80", "40", "-"):
                out = asyncio.run(wiz.step(m))
            self.assertIn("ready to build", out)
            self.assertEqual(wiz.state, "confirm")
            self.assertEqual(wiz.mode, "open")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_custom_compartments_drive_the_interior(self):
        from box_types import circular
        import cubby as cubby_mod
        tmp = tempfile.mkdtemp()
        try:
            wiz = circular.Wizard({"boxes_root": tmp}, logging.getLogger("test"))
            wiz.start()
            out = None
            for m in ("cc", "cubby", "60x40, 60x20", "go", "30", "-"):
                out = asyncio.run(wiz.step(m))
                if isinstance(out, tuple):
                    out = out[0]
            self.assertEqual(wiz.mode, "sizes")
            self.assertIn("ready to build", out)
            self.assertIn("cubby", out.lower())

            # The whole point: the tray the wizard will build must actually drop
            # into the bore it derived, corners and all.
            _, span_w, span_l = wiz.layout_pockets
            tray_diag = math.hypot(span_w + 2 * cubby_mod.WALL,
                                   span_l + 2 * cubby_mod.WALL)
            self.assertLessEqual(tray_diag, wiz.params["Internal_Diameter"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestInserts(unittest.TestCase):
    def setUp(self):
        import boxmeta
        self.tmp = tempfile.mkdtemp()
        self.box = Path(self.tmp) / "case"
        boxmeta.write(self.box, boxmeta.make(
            "case", "rugged", {"width": 160, "length": 100, "height": 40},
            insert_supported=True, corner_radius=5.0))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wiz(self):
        from box_types import inserts
        return inserts.Wizard({"boxes_root": self.tmp}, logging.getLogger("test"))

    def test_choices_lists_only_insert_ready(self):
        from box_types import inserts
        Path(self.tmp, "plain").mkdir()          # no box.json at all
        self.assertEqual(inserts.choices({"boxes_root": self.tmp}), ["case"])

    def test_start_rejects_unknown_box(self):
        out = self._wiz().start("ghost")
        self.assertIn("don't have metadata", out)

    def test_flow_builds_with_the_chosen_layout(self):
        from unittest import mock
        wiz = self._wiz()
        wiz.start("case")
        with mock.patch("cubby.build_equal", return_value=(True, "")) as build, \
             mock.patch("box_types.inserts.render_tray", return_value=([], [])):
            asyncio.run(wiz.step("grid 3 2"))
            asyncio.run(wiz.step("standard"))
            asyncio.run(wiz.step("-"))
            res = asyncio.run(wiz.step("go"))
        text = res[0] if isinstance(res, tuple) else res
        self.assertIn("cubby insert built", text)
        # inner_w/l, height, cols, rows, out, clearance, corner_radius
        args, kwargs = build.call_args
        self.assertEqual(args[3], 3)             # cols
        self.assertEqual(args[4], 2)             # rows

    def test_layout_rejects_over_fine_grid(self):
        wiz = self._wiz()
        wiz.start("case")
        out = asyncio.run(wiz.step("grid 20 20"))
        self.assertIn("minimum", out)
        self.assertEqual(wiz.state, "layout")


class TestInsertsCommand(unittest.TestCase):
    def test_inserts_registers_with_a_box_option(self):
        import discord
        import gutsman
        client = discord.Client(intents=discord.Intents.default())
        cfg = dict(gutsman.DEFAULT_CONFIG, discord_channel_id="123")
        tree = gutsman.build_tree(client, cfg, logging.getLogger("test"), {})
        cmd = next(c for c in tree.get_commands() if c.name == "inserts")
        self.assertEqual([p.name for p in cmd.parameters], ["box"])


if __name__ == "__main__":
    unittest.main()
