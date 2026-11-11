"""Bot-facing menus: everything a chat bot needs to show buttons instead of
making the user type edits. Plain JSON-able dicts — the bot renders them,
the user taps, the bot calls run().

    from protobot import ui
    r = sketch_to_part(...)
    ui.feature_menu(r)     # one entry per label (E#/C#/H#) with its actions
    ui.part_menu(r)        # undo / revert / symmetry / straighten / finish / groups
    try: ...
    except ConflictError as e:
        ui.conflict_menu(e)            # [Cancel] [Override …]

An action is either ready to run:
    {"id": "E3:straight", "text": "Straight", "do": "edit", "edit": "E3 straight"}
or needs input first ("fields" describe what to ask; fill with run(values=...)):
    {"id": "E3:length", "text": "Set length…", "do": "edit",
     "template": "E3 is {mm}mm",
     "fields": [{"name": "mm", "type": "number", "prompt": "Length of E3 (mm)",
                 "default": 30.1}]}

run(job_dir, action, values) performs it and returns the new Result
(or raises ConflictError / ValueError with a message to show).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import fasteners as fx
from .pipeline import Result, history, revert_part, revise_part, undo_part

DIRS = ["up", "down", "left", "right"]


def _num(v: str, key: str):
    m = re.search(re.escape(key) + r"\s*([+-]?\d+(\.\d+)?)", v)
    return float(m.group(1)) if m else None


def _mm(name="mm", prompt="Size (mm)", default=None):
    f = {"name": name, "type": "number", "prompt": prompt}
    if default is not None:
        f["default"] = round(default, 1)
    return f


def visible(field: dict, values: dict) -> bool:
    """A field with "when": {other_field: [allowed values]} is only asked
    once that other field holds one of those values."""
    return all((values or {}).get(k) in allowed
               for k, allowed in field.get("when", {}).items())


def _screw_fields(first: str, first_label: str, slot: bool = False) -> list[dict]:
    """Size dropdown (with a 'plain'/'none' first entry, and 'slot' when
    adding), then — only for an M size — screw head / nut / both, and which
    face."""
    m = {"type": fx.SIZES}
    extra = ["slot"] if slot else []
    return [
        {"name": "type", "type": "choice", "choices": [first] + extra + fx.SIZES,
         "labels": {first: first_label, "slot": "Slot (stretched hole)",
                    **{s: f"{s} screw (Ø{fx.clearance(s):g} hole)" for s in fx.SIZES}},
         "prompt": "What kind of hole?"},
        {"name": "style", "type": "choice", "choices": list(fx.STYLES), "when": m,
         "labels": {"screw": "Screw head sits flush (pan or countersunk)",
                    "nut": "Hex nut pocket",
                    "both": "Screw head one side, nut the other"},
         "prompt": "Screw head, nut, or both?"},
        {"name": "face", "type": "choice", "choices": list(fx.FACES), "when": m,
         "labels": {"top": "Top face (the head / nut goes on top)",
                    "bottom": "Bottom face (against the bed)"},
         "prompt": "Which face?"},
    ]


def _roomy_spot(r: Result):
    """Centre of the biggest clear circle on the part (where a new hole
    fits best), from the version's outline.json; None if unavailable."""
    try:
        from shapely.geometry import Polygon
        from shapely.ops import polylabel
        o = json.loads((Path(r.job_dir) / "outline.json").read_text())
        p = polylabel(Polygon(o["outer"], o["holes"]).buffer(0), 0.1)
        return round(p.x, 1), round(p.y, 1)
    except Exception:  # noqa: BLE001 (a default is a convenience)
        return None


def _labels(r: Result, kind=None):
    return [x["label"] for x in r.labels if kind is None or x["kind"] == kind]


def _act(label, key, text, edit=None, template=None, fields=None, **extra):
    a = {"id": f"{label}:{key}", "text": text, "do": "edit"}
    if edit:
        a["edit"] = edit
    else:
        a["template"], a["fields"] = template, fields or []
    a.update(extra)
    return a


def feature_menu(r: Result) -> list[dict]:
    """One entry per labelled feature: what it is, its exact value, whether
    a requirement locks it, and the actions that make sense for it."""
    others = _labels(r)
    out = []
    for row in r.labels:
        L, kind, val = row["label"], row["kind"], row["value"]
        acts = []
        if kind == "edge":
            m = re.search(r"\((\d+(\.\d+)?) long\)", val) or re.search(r"(\d+(\.\d+)?) long", val)
            length = float(m.group(1)) if m else None
            acts += [
                _act(L, "straight", "Straight", f"{L} straight"),
                _act(L, "level", "Level", f"{L} level"),
                _act(L, "plumb", "Upright", f"{L} plumb"),
                _act(L, "arc", "Make it an arc", f"{L} arc"),
                _act(L, "raw", "As traced", f"{L} raw"),
                _act(L, "length", "Set length…", template=f"{L} is {{mm}}mm",
                     fields=[_mm(prompt=f"Length of {L} (mm)", default=length)],
                     requirement=True),
            ]
            if val.startswith(("X =", "Y =")):
                acts.append(_act(L, "at", "Set position…", template=f"{L} at {{mm}}",
                                 fields=[_mm(prompt=f"{val[0]} position of {L} (mm)",
                                             default=_num(val, val[0] + " ="))],
                                 requirement=True))
            acts += [
                _act(L, "move", "Move…", template=f"{L} {{dir}} {{mm}}",
                     fields=[{"name": "dir", "type": "choice", "choices": DIRS + ["out", "in"],
                              "prompt": "Which way?"}, _mm(prompt="How far (mm)")]),
                _act(L, "dist", "Distance to…", template=f"{L} to {{other}} is {{mm}}",
                     fields=[{"name": "other", "type": "choice",
                              "choices": [o for o in others if o != L and o[0] in "EH"],
                              "prompt": f"Distance from {L} to which feature?"},
                             _mm(prompt="Distance (mm)")], requirement=True),
            ]
        elif kind == "corner":
            r_now = _num(val, "R")
            acts += [
                _act(L, "radius", "Round it (R)…", template=f"{L} R{{mm}}",
                     fields=[_mm(prompt=f"Radius of {L} (mm)", default=r_now)]),
                _act(L, "sharp", "Sharp", f"{L} sharp"),
                _act(L, "chamfer", "Chamfer (flat)", f"{L} straight"),
                _act(L, "raw", "As traced", f"{L} raw"),
            ]
        elif kind == "hole":
            d_now = _num(val, "Ø")
            c = re.search(r"center \(([-\d.]+), ([-\d.]+)\)", val)
            sl = re.match(r"([\d.]+) wide × ([\d.]+) long, ([\d.]+)°", val)
            if sl:
                acts.append(_act(L, "slot", "Slot size…",
                                 template=f"{L} slot {{w}} {{l}} angle {{angle}}",
                                 fields=[_mm("w", "Slot width (mm)", float(sl.group(1))),
                                         _mm("l", "Slot length, end to end (mm)",
                                             float(sl.group(2))),
                                         _mm("angle", "Angle (0 = along X, 90 = up)",
                                             float(sl.group(3)))]))
            acts += [
                _act(L, "circle", "Make round (Ø)…" if sl else "Diameter…",
                     template=f"{L} circle {{mm}}",
                     fields=[_mm(prompt=f"Diameter of {L} (mm)",
                                 default=float(sl.group(1)) if sl else d_now)]),
                _act(L, "at", "Set centre…", template=f"{L} at {{x}}, {{y}}",
                     fields=[_mm("x", "Centre X (mm)", float(c.group(1)) if c else None),
                             _mm("y", "Centre Y (mm)", float(c.group(2)) if c else None)],
                     requirement=True),
                _act(L, "move", "Move…", template=f"{L} {{dir}} {{mm}}",
                     fields=[{"name": "dir", "type": "choice", "choices": DIRS,
                              "prompt": "Which way?"}, _mm(prompt="How far (mm)")]),
                _act(L, "dist", "Distance to…", template=f"{L} to {{other}} is {{mm}}",
                     fields=[{"name": "other", "type": "choice",
                              "choices": [o for o in others if o != L and o[0] in "EH"],
                              "prompt": f"Distance from {L} to which feature?"},
                             _mm(prompt="Distance (mm)")], requirement=True),
                _act(L, "centre", "On centreline", f"{L} on centreline", requirement=True),
            ]
            if not sl:
                acts.append(_act(L, "screw", "Screw / nut…", fields=_screw_fields(
                    "none", "None (plain round hole)")))
            acts += [
                _act(L, "remove", "Remove", f"{L} remove"),
                _act(L, "raw", "As traced", f"{L} raw"),
            ]
        out.append({"label": L, "kind": kind, "desc": row["desc"], "value": val,
                    "locked": L in r.locked, "actions": acts})
    return out


def part_menu(r: Result) -> list[dict]:
    """Whole-part actions: versions, toggles, edge finish, hole groups."""
    hist = history(r.job_dir)
    older = [h["version"] for h in hist if h["version"] != r.version]
    acts = [
        {"id": "undo", "text": "Undo", "do": "undo", "enabled": r.version > 1},
        {"id": "revert", "text": "Go back to…", "do": "revert", "enabled": bool(older),
         "fields": [{"name": "version", "type": "choice", "choices": older,
                     "labels": {h["version"]: f"v{h['version']}: {h['note']}" for h in hist},
                     "prompt": "Which version?"}]},
        {"id": "symmetry", "text": "Symmetry…", "do": "symmetry", "current": r.symmetry,
         "fields": [{"name": "symmetry", "type": "choice",
                     "choices": ["off", "lr", "tb", "both"],
                     "labels": {"off": "None", "lr": "Left ↔ right",
                                "tb": "Top ↔ bottom", "both": "Both"},
                     "prompt": "Which way does the part mirror?"}]},
        {"id": "straighten", "text": "Straighten: " + ("on" if r.straighten else "off"),
         "do": "straighten", "current": r.straighten},
        {"id": "finish", "text": "Edge finish…", "do": "edit",
         "template": "{side} edges {kind} {mm}",
         "fields": [{"name": "side", "type": "choice", "choices": ["all", "top", "bottom"],
                     "prompt": "Which edges?"},
                    {"name": "kind", "type": "choice", "choices": ["R", "chamfer", "sharp"],
                     "prompt": "Rounded, chamfered or sharp?"},
                    _mm(prompt="Size (mm, 0 for sharp)", default=1.0)],
         "current": r.finish},
    ]
    spot = _roomy_spot(r) or (round(r.length / 2, 1), round(r.width / 2, 1))
    acts.append({"id": "addhole", "text": "Add hole…", "do": "edit",
                 "fields": _screw_fields("plain", "Plain round hole (any size)",
                                         slot=True) + [
                     dict(_mm("d", "Hole diameter (mm)", 5.0), when={"type": ["plain"]}),
                     dict(_mm("w", "Slot width (mm)", 5.0), when={"type": ["slot"]}),
                     dict(_mm("l", "Slot length, end to end (mm)", 15.0),
                          when={"type": ["slot"]}),
                     dict(_mm("angle", "Slot angle (0 = along X, 90 = up)", 0.0),
                          when={"type": ["slot"]}),
                     _mm("x", "Centre X (mm, from the left)", spot[0]),
                     _mm("y", "Centre Y (mm, from the bottom)", spot[1])]})
    acts.append({"id": "scale", "text": "Scale…", "do": "edit",
                 "fields": [
                     {"name": "by", "type": "choice", "choices": ["length", "width", "percent"],
                      "labels": {"length": f"Set the length (now {r.length:g} mm)",
                                 "width": f"Set the width (now {r.width:g} mm)",
                                 "percent": "By a percentage"},
                      "prompt": "Scale by which size? (proportions stay)"},
                     dict(_mm("length", "New length (mm)", r.length), when={"by": ["length"]}),
                     dict(_mm("width", "New width (mm)", r.width), when={"by": ["width"]}),
                     dict(_mm("pct", "Percent (100 = same size)", 100.0),
                          when={"by": ["percent"]})]})
    holes = _labels(r, "hole")
    if len(holes) >= 2:
        acts.append({"id": "samesize", "text": "Make holes the same size…", "do": "edit",
                     "template": "{holes} same size",
                     "fields": [{"name": "holes", "type": "multi", "choices": holes,
                                 "min": 2, "prompt": "Which holes? (first sets the size)"}]})
        acts.append({"id": "apart", "text": "Hole spacing…", "do": "edit",
                     "template": "{holes} apart {mm}",
                     "fields": [{"name": "holes", "type": "multi", "choices": holes,
                                 "min": 2, "max": 2, "prompt": "Which two holes?"},
                                _mm(prompt="Centre-to-centre (mm)")], "requirement": True})
    if len(holes) >= 3:
        acts.append({"id": "even", "text": "Space holes evenly…", "do": "edit",
                     "template": "{holes} evenly spaced",
                     "fields": [{"name": "holes", "type": "multi", "choices": holes,
                                 "min": 3, "prompt": "Which holes, in order?"}],
                     "requirement": True})
    return acts


def conflict_menu(err) -> list[dict]:
    """Buttons for a ConflictError: always Cancel; Override when allowed."""
    acts = [{"id": "cancel", "text": "Cancel (keep current version)", "do": "cancel"}]
    if err.can_override and err.conflicts:
        if err.together:
            acts.append({"id": "override", "text": "Override " + " + ".join(
                c["label"] for c in err.conflicts), "do": "override",
                "override": [c["label"] for c in err.conflicts]})
        else:
            for c in err.conflicts:
                acts.append({"id": f"override:{c['label']}",
                             "text": f"Override {c['label']} ({c['desc']})",
                             "do": "override", "override": [c["label"]]})
    return acts


def fill(action: dict, values: dict | None = None) -> str:
    """The edit text for an action, with its fields filled in."""
    if "edit" in action:
        return action["edit"]
    v = dict(values or {})
    for f in action.get("fields", []):
        if not visible(f, v):
            continue
        if f["name"] not in v:
            if "default" in f:
                v[f["name"]] = f["default"]
            else:
                raise ValueError(f"{action['text']}: need a value for {f['prompt']!r}")
        if f["type"] == "multi":
            items = v[f["name"]]
            if isinstance(items, str):
                items = items.split()
            if len(items) < f.get("min", 1) or len(items) > f.get("max", 99):
                raise ValueError(f"{action['text']}: pick {f.get('min', 1)}"
                                 f"{'' if f.get('max') == f.get('min') else '+'} items")
            v[f["name"]] = " ".join(items)
    if action["id"] == "addhole":
        at = f"at {v['x']:g}, {v['y']:g}"
        if v["type"] == "plain":
            return f"add hole {v['d']:g} {at}"
        if v["type"] == "slot":
            return f"add slot {v['w']:g} {v['l']:g} angle {v['angle']:g} {at}"
        return f"add {v['type']} {v['style']} {v['face']} {at}"
    if action["id"].endswith(":screw"):
        lab = action["id"].split(":")[0]
        if v["type"] == "none":
            return f"{lab} plain"
        return f"{lab} {v['type']} {v['style']} {v['face']}"
    if action["id"] == "scale":
        if v["by"] == "percent":
            return f"scale {v['pct']:g}%"
        return (f"scale to {v['length']:g} long" if v["by"] == "length"
                else f"scale to {v['width']:g} wide")
    if action["id"] == "finish":
        if v["kind"] == "sharp":
            return f"{v['side']} edges sharp"
        return (f"{v['side']} edges R{v['mm']}" if v["kind"] == "R"
                else f"{v['side']} edges chamfer {v['mm']}")
    return action["template"].format(**v)


def run(job_dir: str, action: dict, values: dict | None = None, *,
        pending: str | None = None, preview: bool = True) -> Result | None:
    """Do what a button says. For an Override button, pass the edit that
    caused the conflict as `pending`. Cancel returns None (nothing changes)."""
    do = action["do"]
    if do == "cancel":
        return None
    if do == "override":
        if not pending:
            raise ValueError("override needs the edit that caused the conflict (pending=...)")
        return revise_part(job_dir, pending, override=action["override"], preview=preview)
    if do == "undo":
        return undo_part(job_dir, preview=preview)
    if do == "revert":
        return revert_part(job_dir, int((values or {})["version"]), preview=preview)
    if do == "symmetry":
        sym = (values or {})["symmetry"]
        return revise_part(job_dir, "", symmetry=None if sym == "off" else sym,
                           preview=preview, note=f"symmetry {sym}")
    if do == "straighten":
        on = not action.get("current", False) if values is None else bool(values.get("on"))
        return revise_part(job_dir, "", straighten=on, preview=preview,
                           note=f"straighten {'on' if on else 'off'}")
    return revise_part(job_dir, fill(action, values), preview=preview)
