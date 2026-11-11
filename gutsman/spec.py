#!/usr/bin/env python3
"""spec.py — the interior-first "containment" front-end.

The project's core promise: the user describes the *space or things* they need to
hold, and the box is built around that. This module turns what a user actually
knows — "I need a 60×40 and a 60×20 compartment, 40 deep" — into the numbers a
box wizard needs: the box **interior** and the **tray cells**. The user never
sizes walls, dividers, or the shell.

It is a shared *primitive*, opt-in: a wizard calls it during its opening and then
asks only its own shell questions. It is not a controller and not a schema every
box type must fit (see CLAUDE.md).

Phase 1 supports listed compartments laid out in a single row along the length
(Y), all sharing the interior width (X) — which is exactly how people describe a
divided box. Grid / custom-width layouts and object+clearance derivation are
later phases (see ROADMAP.md); the cell-driven geometry underneath already
supports them.
"""
import math
import re

import cubby

# Vertical build-up from a compartment's usable depth to the box base cavity:
# the tray needs a floor under the pocket, and the base needs a hair over the
# tray so it drops in.
LID_GAP = 0.5


SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)")


def parse_compartments(text: str):
    """Parse a compartment list into an expanded list of (width, length) cells.

    Accepts `x`/`X`/`×` and commas/semicolons/newlines between entries, plus a
    count shorthand: a bare number multiplies the size that follows it, so `7,
    4x20` means seven 4×20 compartments. Counts and explicit sizes can mix:
    `3, 50x30, 60x40` → three 50×30 plus one 60×40.

    Returns (cells, None) or (None, error).
    """
    cells = []
    pending = None                       # a count waiting for its size
    for chunk in re.split(r"[,\n;]+", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if re.fullmatch(r"\d+", chunk):                 # a bare count
            if pending is not None:
                return None, f"`{pending}` needs a size after it, e.g. `{pending}, 40x20`."
            pending = int(chunk)
            if pending < 1:
                return None, "the count must be at least 1."
            continue
        m = re.fullmatch(SIZE_RE, chunk)
        if not m:
            return None, (f"couldn't read `{chunk}` — use `WxL` (e.g. `60x40`), or "
                          "`N, WxL` for N equal compartments (e.g. `7, 4x20`).")
        w, l = float(m.group(1)), float(m.group(2))
        if w <= 0 or l <= 0:
            return None, "compartment sizes must be positive."
        cells.extend([(w, l)] * (pending or 1))
        pending = None
    if pending is not None:
        return None, f"`{pending}` needs a size after it, e.g. `{pending}, 40x20`."
    if not cells:
        return None, "list at least one compartment, e.g. `60x40, 60x20`."
    return cells, None


GRID_RE = re.compile(
    r"grid\s+(\d+)\s*[xX×]\s*(\d+)\s+of\s+"
    r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def normalize_count_form(text: str) -> str:
    """`6 of 40x20` is the friendly spelling of the count shorthand `6, 40x20`."""
    of = re.fullmatch(r"(\d+)\s+of\s+(\d+(?:\.\d+)?\s*[xX×]\s*\d+(?:\.\d+)?)",
                      text.strip(), re.IGNORECASE)
    return f"{of.group(1)}, {of.group(2)}" if of else text.strip()


def parse_explicit_grid(text: str):
    """Parse only the explicit `grid CxR of WxL` form.

    Returns (layout, None) for a match, (None, error) for a malformed grid, and
    (None, None) when the text simply isn't a grid — the caller then treats it
    as a compartment list and packs it.

    This is the one input that pins the *arrangement* as well as the sizes: the
    user asked for exactly 3 columns × 2 rows, so it is honoured verbatim rather
    than repacked. Everything else routes through `auto_pack`, because a listed
    compartment is a promise about the compartment, not about where it sits.
    """
    m = GRID_RE.fullmatch(normalize_count_form(text))
    if not m:
        return None, None
    cols, rows = int(m.group(1)), int(m.group(2))
    w, l = float(m.group(3)), float(m.group(4))
    if cols < 1 or rows < 1:
        return None, "a grid needs at least 1×1."
    if w <= 0 or l <= 0:
        return None, "compartment sizes must be positive."
    return ([w] * cols, [l] * rows, f"{cols}×{rows} grid of {w:g}×{l:g} mm"), None


def interior_for(col_widths, row_lengths, depth: float,
                 clearance: float = 0.15) -> dict:
    """Derive the box interior that holds this tray, with fit clearance.

    Horizontals grow from the tray's outer size + clearance; the height carries
    the tray floor and a small drop-in gap over the requested compartment depth.
    Rounded up to whole mm because the box wizards work in integer interiors.
    """
    ow, ol = cubby.tray_outer(col_widths, row_lengths)
    return {
        "width": math.ceil(ow + 2 * clearance),
        "length": math.ceil(ol + 2 * clearance),
        "height": math.ceil(depth + cubby.FLOOR + LID_GAP),
    }


ROW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(.+)$")
DEFAULT_CELL = 40.0                       # cell width when a row is a bare count


def parse_rows(text: str):
    """Parse a ragged 'shelf' layout: rows of `length: division`.

    Each row runs across the full width; `length` is its front→back extent and
    `division` is either a count (even split) or a comma list of proportional
    widths. Rows are separated by newlines or `;`. Returns (rows, None) where
    rows is a list of (length, count|[widths]), or (None, error).
        40: 1        one full-width cell, 40 mm deep
        30: 3        three even cells, 30 mm deep
        20: 60, 30   two cells 2:1, 20 mm deep   → 6 cubbies total (odd is fine)
    """
    rows = []
    for line in re.split(r"[;|\n]+", text):
        line = line.strip()
        if not line:
            continue
        m = ROW_RE.match(line)
        if not m:
            return None, (f"couldn't read row `{line}` — write each row as "
                          "`front-to-back : cells`, e.g. `30: 3` (three even) or "
                          "`40: 60, 30` (widths 60 and 30).")
        length = float(m.group(1))
        if length <= 0:
            return None, "a row length must be positive."
        div = m.group(2).strip()
        if re.fullmatch(r"\d+", div):
            rows.append((length, int(div)))
        else:
            widths, err = parse_numbers(div)
            if err:
                return None, err
            rows.append((length, widths))
    if not rows:
        return None, "list at least one row, e.g. `40: 1` then `30: 3`."
    return rows, None


def _orientations(cell, rotate: bool):
    """The ways a cell may be laid down: as given, and turned 90° if allowed."""
    w, l = cell
    return [(w, l)] if (not rotate or w == l) else [(w, l), (l, w)]


def _shelf_pack(cells, target_w, divider, rotate: bool = False):
    """Shelf-pack cells into rows no wider than target_w.

    Sizes are never changed. With `rotate`, the packer may turn a cell 90°: a
    60×20 pocket and a 20×60 pocket are the same hole, so which way it lies is
    the packer's business, not a promise to keep — and letting it choose is what
    lets a mixed list collapse into a compact block instead of a strip. Only the
    auto path passes rotate=True; hand-drawn layouts are placed exactly as
    written.

    Returns (shelves, span_w, span_l).
    """
    # Order by longest side so the bulky cells are placed while shelves are
    # still empty. Rotation-invariant, unlike sorting on length alone.
    order = sorted(range(len(cells)),
                   key=lambda i: (-max(cells[i]), -min(cells[i])))
    shelves = []
    for i in order:
        best = None                       # (score, shelf, w, l)
        for w, l in _orientations(cells[i], rotate):
            for s in shelves:
                if s["w"] + divider + w > target_w + 1e-6:
                    continue
                # Prefer the orientation that makes this row no taller; then
                # the shorter one; then the wider, to fill the row out.
                key = (max(0.0, l - s["h"]), l, -w)
                if best is None or key < best[0]:
                    best = (key, s, w, l)
        if best is not None:
            _, shelf, w, l = best
        else:
            # Nothing fits an open row — start one. Pick the orientation that
            # fits the target width, shortest first, so rows stay low.
            fits = sorted((l, w) for w, l in _orientations(cells[i], rotate)
                          if w <= target_w + 1e-6)
            if fits:
                l, w = fits[0]
            else:                          # wider than the target either way;
                w, l = min(_orientations(cells[i], rotate))   # the sweep copes
            shelf = {"items": [], "w": 0.0, "h": 0.0}
            shelves.append(shelf)
        x = shelf["w"] + (divider if shelf["items"] else 0.0)
        shelf["items"].append((x, w, l))
        shelf["w"] = x + w
        shelf["h"] = max(shelf["h"], l)
    span_w = max(s["w"] for s in shelves)
    span_l = sum(s["h"] for s in shelves) + (len(shelves) - 1) * divider
    return shelves, span_w, span_l


def _count_rotated(cells, shelves) -> int:
    """How many cells ended up turned 90° from the way they were listed.

    Worth reporting: the user asked for `50x30` and the preview will label it
    `30x50`, so the summary says so rather than letting them think the bot
    mangled their sizes.
    """
    remaining = list(cells)
    rotated = 0
    for s in shelves:
        for _, w, l in s["items"]:
            if (w, l) in remaining:
                remaining.remove((w, l))
            elif (l, w) in remaining:
                remaining.remove((l, w))
                rotated += 1
    return rotated


def auto_pack(cells, divider: float = cubby.DIVIDER, rotate: bool = True):
    """Arrange a flat list of (w, l) compartments into a compact, near-square tray.

    Sizes are kept exactly as given — a listed compartment is a promise — but by
    default a cell may be **turned 90°**, since a 60×20 pocket and a 20×60
    pocket are the same hole. Pass rotate=False to honour the listed orientation
    too (what the hand-drawn layouts effectively do by not coming through here).

    We try a sweep of row widths, shelf-pack for each, and keep the arrangement
    with the smallest bounding box (tie-break: closest to square) — so cells that
    can share a row do, instead of everything ending up in one tall column. Slack
    becomes centred wall, not a void.

    Returns (pockets, span_w, span_l, note).
    """
    # With rotation every cell can present its narrow side, so the tightest
    # possible row is set by the widest *narrow* side.
    min_w = max(min(c) if rotate else c[0] for c in cells)
    total_w = (sum(max(c) if rotate else c[0] for c in cells)
               + (len(cells) - 1) * divider)
    # Every width a cell could present, which is both sides when it may turn.
    sides = set()
    for w, l in cells:
        sides.add(w)
        if rotate:
            sides.add(l)
    candidates = {min_w, total_w, math.sqrt(sum(w * l for w, l in cells))}
    steps = 30
    for k in range(steps + 1):                       # sweep narrowest → one row
        candidates.add(min_w + (total_w - min_w) * k / steps)
    # exact "these two share a row" breakpoints, so a tight fit isn't missed
    for a in sides:
        candidates.add(a)
        for b in sides:
            candidates.add(a + divider + b)

    # Try each target width with rotation allowed *and* forbidden, keeping the
    # best of everything. Shelf packing is greedy, so permitting rotation can
    # occasionally send the placement down a worse path than the as-listed one;
    # searching both makes rotation strictly additive — it can only ever improve
    # the result, never degrade it.
    modes = (False, True) if rotate else (False,)
    best = None
    for tw in sorted(c for c in candidates if c >= min_w - 1e-6):
        for rot in modes:
            shelves, span_w, span_l = _shelf_pack(cells, tw, divider, rot)
            # Shrink the tray to a compact square by minimising the *longer*
            # side first (what a print bed and the eye both care about), then
            # breaking ties by total footprint so the least-wasteful packing
            # wins. Scoring by bounding-box *aspect* instead is a trap: a
            # perfectly square box is often only reachable by parking a short
            # cell in a tall row and eating the void — squareness with a
            # wasteful interior, the opposite of small.
            # Final tie-break prefers the as-listed orientation, so cells are
            # only ever turned when it actually buys something. Without it a
            # 7×(4×20) row ties with its own transpose and the packing reports
            # "7 turned 90°" for an identical tray — alarming, for no gain.
            key = (round(max(span_w, span_l), 3), round(span_w * span_l, 2),
                   int(rot))
            if best is None or key < best[0]:
                best = (key, shelves, span_w, span_l)
    _, shelves, span_w, span_l = best

    pockets, y = [], 0.0
    for shelf in shelves:
        off = (span_w - shelf["w"]) / 2.0            # centre → slack becomes wall
        for x, w, l in shelf["items"]:
            pockets.append((round(x + off, 3), round(y, 3), round(w, 3), round(l, 3)))
        y += shelf["h"] + divider
    note = _pack_note(shelves, _count_rotated(cells, shelves))
    return pockets, round(span_w, 3), round(y - divider, 3), note


def _pack_note(shelves, rotated: int = 0) -> str:
    """Describe a packing the way a person would say it.

    Reads the cells as *placed*, not as listed, so a turned cell is described at
    the size it will actually be printed. When the result happens to be a plain
    grid it is worth saying so — "3×2 grid of 40×20 mm" reads as a deliberate
    shape, whereas "6 compartments in 2 rows" reads like the bot gave up and
    shuffled them. Same arrangement either way; only the wording changes.
    """
    placed = [(w, l) for s in shelves for _, w, l in s["items"]]
    n = len(placed)
    turned = f" ({rotated} turned 90°)" if rotated else ""
    if n == 1:
        w, l = placed[0]
        return f"one {w:g}×{l:g} mm compartment{turned}"
    counts = {len(s["items"]) for s in shelves}
    if len(set(placed)) == 1 and len(counts) == 1:
        w, l = placed[0]
        return f"{counts.pop()}×{len(shelves)} grid of {w:g}×{l:g} mm{turned}"
    return (f"{n} compartments in {len(shelves)} row"
            f"{'s' if len(shelves) != 1 else ''}{turned}")


def parse_cell_rows(text: str):
    """Parse a grid drawn as text — one line per row, cells left→right.

    Each line is a comma list of `WxL` cells; the line becomes a row whose
    front-to-back length is the cells' length and whose columns are their widths.
    Rows are separated by newlines, `;` or `|`. Cell sizes are kept **exact** —
    the box is sized to the widest row and a narrower row is centred, its slack
    becoming thicker (solid) wall, never an empty sliver.
        20x20, 20x20      two 20-wide cells, 20 front-to-back
        40x50             one 40-wide cell, 50 front-to-back (centred, exact)
    Returns (rows, None) as (length, [widths]) tuples, or (None, error).
    """
    rows = []
    for line in re.split(r"[\n;|]+", text):
        line = line.strip()
        if not line:
            continue
        widths, lengths = [], []
        for chunk in line.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = re.fullmatch(SIZE_RE, chunk)
            if not m:
                return None, (f"couldn't read `{chunk}` — draw each cell as "
                              "`WxL`, e.g. `20x20`, cells separated by commas.")
            w, l = float(m.group(1)), float(m.group(2))
            if w <= 0 or l <= 0:
                return None, "cell sizes must be positive."
            widths.append(w)
            lengths.append(l)
        if widths:
            rows.append((max(lengths), widths))
    if not rows:
        return None, "draw at least one row, e.g. `20x20, 20x20`."
    return rows, None


def row_pockets(rows, divider: float = cubby.DIVIDER):
    """Turn ragged rows into (pockets, span_w, span_l, note).

    Every row spans the same interior width — the widest row's requirement (or
    the widest even-count row at DEFAULT_CELL) — with each row's cells scaled to
    fill it. Pockets are [x, y, w, l], interior-local, 0,0 at the inside corner.
    """
    spans = [sum(c) + (len(c) - 1) * divider for _, c in rows if isinstance(c, list)]
    counts = [c for _, c in rows if isinstance(c, int)]
    span_w = max(spans) if spans else max(
        n * DEFAULT_CELL + (n - 1) * divider for n in counts)

    pockets, y, total = [], 0.0, 0
    for length, cells in rows:
        if isinstance(cells, list):
            widths = list(cells)          # EXACT — a listed size is honoured
        else:
            # a count has no size of its own, so it evenly fills the box width
            widths = [(span_w - (cells - 1) * divider) / cells] * cells
        # Centre the row: any slack under the widest row becomes symmetric wall
        # (thicker sides), never a lopsided empty sliver.
        row_w = sum(widths) + (len(widths) - 1) * divider
        x = (span_w - row_w) / 2.0
        for w in widths:
            pockets.append((round(x, 3), round(y, 3), round(w, 3), round(length, 3)))
            x += w + divider
        total += len(widths)
        y += length + divider

    span_l = sum(l for l, _ in rows) + (len(rows) - 1) * divider
    note = f"{len(rows)} rows: " + " / ".join(
        f"{l:g}mm×{len(c) if isinstance(c, list) else c}" for l, c in rows)
    return pockets, round(span_w, 3), round(span_l, 3), note


def grid_pockets(col_widths, row_lengths, divider: float = cubby.DIVIDER):
    """Turn a regular grid into (pockets, span_w, span_l) — the same pocket
    representation ragged layouts use, so building and preview share one path."""
    pockets, y = [], 0.0
    for rl in row_lengths:
        x = 0.0
        for cw in col_widths:
            pockets.append((round(x, 3), round(y, 3), round(cw, 3), round(rl, 3)))
            x += cw + divider
        y += rl + divider
    span_w = sum(col_widths) + (len(col_widths) - 1) * divider
    span_l = sum(row_lengths) + (len(row_lengths) - 1) * divider
    return pockets, round(span_w, 3), round(span_l, 3)


def interior_from_span(span_w: float, span_l: float, depth: float,
                       clearance: float = 0.15) -> dict:
    """Box interior for a tray of the given span (ragged layouts). Same
    build-up as interior_for, but the span is passed directly."""
    return {
        "width": math.ceil(span_w + 2 * cubby.WALL + 2 * clearance),
        "length": math.ceil(span_l + 2 * cubby.WALL + 2 * clearance),
        "height": math.ceil(depth + cubby.FLOOR + LID_GAP),
    }


def parse_numbers(text: str):
    """Parse a comma/space list of positive numbers. (nums, None) or (None, err)."""
    nums = []
    for chunk in re.split(r"[,\s]+", text.strip()):
        if not chunk:
            continue
        try:
            v = float(chunk)
        except ValueError:
            return None, f"couldn't read `{chunk}` — use numbers like `60, 40, 30`."
        if v <= 0:
            return None, "sizes must be positive."
        nums.append(v)
    if not nums:
        return None, "list at least one size, e.g. `60, 40, 30`."
    return nums, None


class LayoutStep:
    """One turn of the LayoutBuilder: what to reply, whether to show a preview,
    and whether the layout is finalized (proceed with the wizard)."""
    __slots__ = ("reply", "preview", "done")

    def __init__(self, reply, preview=False, done=False):
        self.reply, self.preview, self.done = reply, preview, done


class LayoutBuilder:
    """A small shared sub-conversation for designing a compartment grid.

    Quick forms (`60x40, 60x20`, a count `7, 4x20`, `grid 3x2 of 40x20`) resolve
    at once; replying `grid` opens the two-axis path — column widths, then row
    lengths — for full control. Either way it lands on a preview the wizard
    renders, and the user replies `go` (proceed) or `edit` (redo). Both box
    wizards embed one, so the layout UX stays identical and lives in one place.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.phase = "entry"
        self.col_widths = None       # set for grid layouts (for display)
        self.row_lengths = None
        self.ragged = False
        self.pockets = None          # canonical build/preview form: [x,y,w,l]
        self.span_w = None
        self.span_l = None
        self.note = ""

    def prompt(self, note: str = "") -> str:
        """One question, and typing the obvious thing gives the best answer.

        This used to ask *how* the user wanted to enter compartments and only
        packed them properly if they picked "auto" first — an extra round trip
        that most people skipped, landing them on the naive arrangement. Now the
        list itself is the answer and it is always packed; the manual forms stay
        available for anyone who wants to place cells by hand.
        """
        return ((note + "\n\n" if note else "")
                + "**What compartments do you need?**\n"
                "List them as `WxL` in mm — I'll pack them into the most compact "
                "tray that fits, turning one 90° if that saves space:\n"
                "`40x20, 40x20, 60x30`  ·  repeats: `6 of 40x20`\n\n"
                "*Want them placed exactly as you say?* Draw one row per line — "
                "nothing gets moved or turned:\n"
                "```\n20x20, 20x20\n20x40\n```\n"
                "…or reply **guide** to be walked through columns and rows.\n\n"
                "*width = side-to-side · length = front-to-back · depth comes next*")

    def _auto_prompt(self, note: str = "") -> str:
        return ((note + "\n\n" if note else "")
                + "**List every compartment** you want, as `WxL` in mm, separated "
                "by commas — e.g. `40x20, 40x20, 60x30, 20x20`. Repeat sizes with "
                "a count too: `6 of 40x20`. I'll arrange them for you.")

    @staticmethod
    def _grid_note(c, r) -> str:
        if len(c) == 1 and len(r) == 1:
            return f"one {c[0]:g}×{r[0]:g} mm compartment"
        if len(set(c)) == 1 and len(set(r)) == 1:
            return f"{len(c)}×{len(r)} grid of {c[0]:g}×{r[0]:g} mm"
        cols = "/".join(f"{w:g}" for w in c)
        rows = "/".join(f"{l:g}" for l in r)
        return f"{len(c)}×{len(r)} grid — cols {cols} × rows {rows} mm"

    def _finalize_note_two_axis(self, row_lengths):
        self._set_grid(self.col_widths, row_lengths,
                       self._grid_note(self.col_widths, row_lengths))

    def _set_grid(self, col_widths, row_lengths, note):
        self.ragged = False
        self.col_widths, self.row_lengths, self.note = col_widths, row_lengths, note
        self.pockets, self.span_w, self.span_l = grid_pockets(col_widths, row_lengths)

    def _set_pockets(self, pockets, span_w, span_l, note):
        self.ragged = True
        self.col_widths = self.row_lengths = None
        self.pockets, self.span_w, self.span_l, self.note = \
            pockets, span_w, span_l, note

    def _set_ragged(self, rows):
        self._set_pockets(*row_pockets(rows))

    def _preview_step(self) -> LayoutStep:
        n = len(self.pockets)
        self.phase = "preview"
        # The tray footprint is the number people actually judge the design by
        # ("is that going to be a metre long?"), so state it rather than making
        # them infer it from the picture.
        return LayoutStep(
            f"**{n}** compartment{'s' if n != 1 else ''} — {self.note}\n"
            f"Tray footprint: **{self.span_w:g} × {self.span_l:g} mm**\n\n"
            "Reply **go** to keep it, or **edit** to start the layout over.",
            preview=True)

    def feed(self, text: str) -> LayoutStep:
        low = text.strip().lower()
        if self.phase == "entry":
            if low in ("auto", "a", "pack", "organize", "organise"):
                self.phase = "auto"
                return LayoutStep(self._auto_prompt())
            if low in ("guide", "grid", "columns", "two-axis"):
                self.phase = "cols"
                return LayoutStep(
                    "**Columns — side-to-side.** How wide is each column, in mm, "
                    "listed left→right? For example `60, 40, 30` makes three "
                    "columns (60, 40 and 30 mm wide).")
            if low in ("manual", "m", "draw", "design"):
                return LayoutStep(self.prompt())      # re-show the two ways
            if ":" in text:                       # `front-to-back: cells` shelf
                rows, err = parse_rows(text)
                if err:
                    return LayoutStep(self.prompt("❌ " + err))
                self._set_ragged(rows)
                return self._preview_step()
            if "\n" in text or ";" in text or "|" in text:   # a grid drawn in rows
                rows, err = parse_cell_rows(text)
                if err:
                    return LayoutStep(self.prompt("❌ " + err))
                self._set_ragged(rows)
                return self._preview_step()
            # `grid CxR of WxL` pins the arrangement, so honour it verbatim.
            res, err = parse_explicit_grid(text)
            if err:
                return LayoutStep(self.prompt("❌ " + err))
            if res:
                self._set_grid(*res)
                return self._preview_step()
            # Anything else is a plain list of compartments — pack it. This is
            # the default path, so the easy input gives the compact tray.
            cells, err = parse_compartments(normalize_count_form(text))
            if err:
                return LayoutStep(self.prompt("❌ " + err))
            self._set_pockets(*auto_pack(cells))
            return self._preview_step()
        if self.phase == "auto":
            cells, err = parse_compartments(normalize_count_form(text))
            if err:
                return LayoutStep(self._auto_prompt("❌ " + err))
            self._set_pockets(*auto_pack(cells))
            return self._preview_step()
        if self.phase == "cols":
            nums, err = parse_numbers(text)
            if err:
                return LayoutStep("❌ " + err + "\n\n**Column widths** (side-to-"
                                  "side), in mm:")
            self.col_widths = nums
            self.phase = "rows"
            return LayoutStep(
                "**Rows — front-to-back.** How far front-to-back is each row, in "
                "mm? For example `20, 25` makes two rows (20 mm then 25 mm). "
                "Every column is split into these same rows.")
        if self.phase == "rows":
            nums, err = parse_numbers(text)
            if err:
                return LayoutStep("❌ " + err + "\n\n**Row lengths**, in mm:")
            self._finalize_note_two_axis(nums)
            return self._preview_step()
        if self.phase == "preview":
            if low in ("go", "continue", "yes", "y", "ok", "keep", "next", "good"):
                return LayoutStep("", done=True)
            if low in ("edit", "redo", "change", "back", "no", "again"):
                self.reset()
                return LayoutStep(self.prompt())
            return LayoutStep("Reply **go** to keep this layout, or **edit** to redo it.")
        return LayoutStep("Something went wrong — type `cancel`.")

    def image_title(self) -> str:
        # ASCII only — the default PIL font tofus × / — glyphs.
        return self.note.replace("×", "x").replace("—", "-")


def tray_height(depth: float) -> float:
    """The tray's own height for a requested usable compartment depth."""
    return depth + cubby.FLOOR
