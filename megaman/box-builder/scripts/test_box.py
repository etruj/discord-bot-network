#!/usr/bin/env python3
"""test_box.py — unit tests that a generated rugged box will actually work.

Reads a manifest.json (written by box_wizard.py) describing the chosen options
and the parts that were exported, then checks the STLs for printability and
dimensional correctness. Point it at an output dir via BOX_OUT (default: ../out):

    BOX_OUT=out_test python3 scripts/test_box.py
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
import warnings

import numpy as np
import trimesh

# trimesh warns when computing the volume of the zero-area facets the Manifold
# backend emits; harmless here (we filter by volume), so quiet it.
warnings.filterwarnings("ignore", category=RuntimeWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rbox_common as rc  # noqa: E402

OUT = os.environ.get("BOX_OUT", os.path.join(rc.ROOT, "out"))
MANIFEST = os.path.join(OUT, "manifest.json")


def _load():
    with open(MANIFEST) as f:
        return json.load(f)


def _mesh(part, m):
    path = os.path.join(OUT, m["files"][part])
    return trimesh.load(path, force="mesh"), path


def _expected_pieces(part, params):
    """How many separate solids this part legitimately prints as."""
    # A draw latch is a two-piece mechanism: the lever and its catch.
    if part == "latch" and params.get("Latch_Type") == "draw":
        return 2
    return 1


def _admesh_int(out, label):
    """Pull the integer after a labelled admesh line, e.g. 'Total ...: 0'."""
    for line in out.splitlines():
        if label in line:
            nums = [int(t) for t in line.replace(":", " ").split() if t.isdigit()]
            if nums:
                return nums[0]
    return None


class BoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(MANIFEST):
            raise unittest.SkipTest(f"no manifest at {MANIFEST} — run the wizard first")
        cls.m = _load()
        cls.p = cls.m["params"]

    # ---- part set -------------------------------------------------------
    def test_expected_parts_present_and_nonempty(self):
        for part, fname in self.m["files"].items():
            path = os.path.join(OUT, fname)
            self.assertTrue(os.path.exists(path), f"missing part: {part} ({fname})")
            self.assertGreater(os.path.getsize(path), 100, f"empty STL: {part}")

    def test_core_parts_always_generated(self):
        for part in ("bottom", "top", "latch"):
            self.assertIn(part, self.m["files"], f"core part not generated: {part}")

    # ---- printability ---------------------------------------------------
    # admesh is the slicer-grade oracle: it tolerates the zero-area facets the
    # OpenSCAD Manifold backend emits, unlike trimesh's strict is_watertight.
    def test_printable_no_holes_no_disconnected(self):
        if not shutil.which("admesh"):
            self.skipTest("admesh not installed (recommended for printability checks)")
        for part in self.m["files"]:
            path = os.path.join(OUT, self.m["files"][part])
            out = subprocess.run(["admesh", path], capture_output=True).stdout.decode(
                "utf-8", errors="replace")
            self.assertIn("No holes need to be filled", out,
                          f"{part} has holes — would not slice cleanly")
            disc = _admesh_int(out, "Total disconnected facets")
            self.assertEqual(disc, 0, f"{part} has {disc} disconnected facets")

    def test_no_unexpected_loose_pieces(self):
        # A part may legitimately consist of more than one solid: a "draw" latch
        # prints as a lever plus a separate catch. What must NOT happen is a
        # part fragmenting into more pieces than the design calls for. Tiny
        # coincident flanges (the seal lip, which the slicer fuses) and
        # zero-area slivers are ignored.
        for part in self.m["files"]:
            mesh, _ = _mesh(part, self.m)
            vols = sorted((abs(c.volume) for c in mesh.split(only_watertight=False)
                           if abs(c.volume) > 1.0), reverse=True)
            self.assertTrue(vols, f"{part} has no solid volume")
            total = sum(vols)
            significant = [v for v in vols if v / total >= 0.05]
            expected = _expected_pieces(part, self.p)
            self.assertLessEqual(len(significant), expected,
                f"{part} split into {len(significant)} substantial pieces "
                f"(expected at most {expected}) — volumes {[round(v) for v in significant]}")
            self.assertGreaterEqual(sum(significant) / total, 0.95,
                f"{part}: {(1 - sum(significant)/total):.0%} of the volume is "
                "loose debris")

    # ---- dimensional correctness ---------------------------------------
    def test_interior_width_not_shrunk(self):
        # The shell must never come out NARROWER than requested interior +
        # walls, or the box would hold less than asked. It may come out wider:
        # the latch/hinge hardware is a fixed size, so on a small box it sets
        # the minimum footprint and the shell is padded to suit.
        mesh, _ = _mesh("bottom", self.m)
        want = rc.outer_width(self.p)
        self.assertGreaterEqual(mesh.extents[0], want - 0.6,
            f"bottom is {mesh.extents[0]:.1f} mm across but the requested "
            f"interior needs at least {want:.1f} mm — interior was shrunk")

    def test_outer_width_matches_request_on_normal_boxes(self):
        # Above the size where fixed hardware dominates, the footprint should
        # track the request closely (protruding latch ribs excluded).
        if float(self.p["Width"]) < 80:
            self.skipTest("small box — footprint is set by fixed-size hardware")
        mesh, _ = _mesh("bottom", self.m)
        self.assertAlmostEqual(mesh.extents[0], rc.outer_width(self.p), delta=1.5,
            msg="bottom side-to-side outer width does not match Width + walls")

    def test_top_and_bottom_mate(self):
        b, _ = _mesh("bottom", self.m)
        t, _ = _mesh("top", self.m)
        self.assertAlmostEqual(b.extents[0], t.extents[0], delta=0.6,
            msg="top and bottom widths differ — halves would not align")
        self.assertAlmostEqual(b.extents[1], t.extents[1], delta=0.6,
            msg="top and bottom lengths differ — halves would not align")

    def test_bottom_height_in_range(self):
        b, _ = _mesh("bottom", self.m)
        lo = self.p["Bottom_Height"] + self.p["Wall_Thickness"] - 1
        hi = self.p["Bottom_Height"] + self.p["Wall_Thickness"] + 2 * self.p["Lip_Thickness"] + 3
        self.assertGreaterEqual(b.extents[2], lo, "bottom too short for requested height")
        self.assertLessEqual(b.extents[2], hi, "bottom taller than expected")

    def test_top_height_in_range(self):
        t, _ = _mesh("top", self.m)
        lo = self.p["Top_Height"] + self.p["Wall_Thickness"] - 1
        hi = self.p["Top_Height"] + self.p["Wall_Thickness"] + 2 * self.p["Lip_Thickness"] + 3
        self.assertGreaterEqual(t.extents[2], lo, "top too short for requested height")
        self.assertLessEqual(t.extents[2], hi, "top taller than expected")

    # ---- feature toggles honoured --------------------------------------
    def test_handle_present_iff_requested(self):
        self.assertEqual("handle" in self.m["files"], bool(self.p["Handle"]),
            "handle generation does not match the Handle option")

    def test_label_present_iff_requested(self):
        self.assertEqual("label" in self.m["files"], bool(self.p["Label"]),
            "label generation does not match the Label option")

    def test_latch_reasonable_size(self):
        latch, _ = _mesh("latch", self.m)
        # a clasp is a small part: every dimension present, and none huge
        self.assertGreater(min(latch.extents), 1.0, "latch degenerate")
        self.assertLess(max(latch.extents), 80.0, "latch unexpectedly large")


if __name__ == "__main__":
    unittest.main(verbosity=2)
