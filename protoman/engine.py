"""Bridge to the part-building engine (the `protobot` package).

The engine lives in engine/protobot/. It knows nothing about Discord and also
runs on its own: python3 -m protobot (from engine/).

This is the only module that imports `protobot`. Everything here is
synchronous and slow (tracing + OpenSCAD take seconds to a minute), so the bot
calls it through asyncio.to_thread, one build at a time.
"""
import importlib
import json
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent


def engine_dir(cfg: dict) -> Path:
    return Path(cfg.get("engine_dir") or (BOT_DIR / "engine"))


def snapshot_info(cfg: dict) -> str:
    """The first line of engine/VERSION, or ""."""
    try:
        return (engine_dir(cfg) / "VERSION").read_text().strip().splitlines()[0]
    except (OSError, IndexError):
        return ""


def load(cfg: dict):
    """Import and return the vendored `protobot` package, or raise ImportError.

    Only engine_dir is put on sys.path, so the bot can't silently pick up
    another installed copy.
    """
    d = engine_dir(cfg)
    if not (d / "protobot" / "__init__.py").is_file():
        raise ImportError(f"no engine snapshot in {d} (is the engine/ folder intact?)")
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    return importlib.import_module("protobot")


def status(cfg: dict) -> str:
    """One line for logs and /protohelp."""
    try:
        load(cfg)
    except ImportError as e:
        return f"not integrated ({e})"
    except Exception as e:  # noqa: BLE001 (snapshot present but broken)
        return f"snapshot present but fails to import: {e}"
    return f"ready ({snapshot_info(cfg) or 'snapshot, no VERSION stamp'})"


def is_conflict(cfg: dict, err: Exception) -> bool:
    return isinstance(err, load(cfg).ConflictError)


# ── builds ────────────────────────────────────────────────────────────────
def build_sketch(cfg: dict, image: str, *, out_dir: str, **kw):
    """Option 1: a drawn silhouette. kw: length, width, thickness, name,
    symmetry, straighten, mode. Raises ValueError with user-safe text."""
    pb = load(cfg)
    return pb.sketch_to_part(image, kw.pop("length"), kw.pop("width"),
                             kw.pop("thickness"), out_dir=out_dir, **kw)


def build_photo(cfg: dict, image: str, *, out_dir: str, **kw):
    """Option 2: a real object on paper. kw: thickness, length, width (both or
    neither), name, paper, symmetry, straighten."""
    pb = load(cfg)
    return pb.photo_to_part(image, kw.pop("thickness"), kw.pop("length", None),
                            kw.pop("width", None), out_dir=out_dir, **kw)


def revise(cfg: dict, job_dir: str, edits: str):
    """Typed edits ("E3 up 2, C2 R5", "undo") as the next version.
    Raises ConflictError (a ValueError) when a target clashes."""
    pb = load(cfg)
    if edits.strip().lower() == "undo":
        return pb.undo_part(job_dir)
    return pb.revise_part(job_dir, edits)


def run_action(cfg: dict, job_dir: str, action: dict, values: dict = None,
               pending: str = None):
    """Do what a menu button says (engine ui.run). None for Cancel."""
    return load(cfg).ui.run(job_dir, action, values, pending=pending)


def fill(cfg: dict, action: dict, values: dict = None) -> str:
    """The edit text an action will apply (for conflict Override)."""
    return load(cfg).ui.fill(action, values)


# ── reading finished jobs ─────────────────────────────────────────────────
def load_result(cfg: dict, job_dir: str):
    """The Result of a finished version, from its result.json."""
    pb = load(cfg)
    data = json.loads((Path(job_dir) / "result.json").read_text())
    fields = set(pb.Result.__dataclass_fields__)
    return pb.Result(**{k: v for k, v in data.items() if k in fields})


def history(cfg: dict, job_dir: str) -> list:
    return load(cfg).history(job_dir)


def menus(cfg: dict, result) -> tuple:
    """(feature_menu, part_menu) for a Result, from the engine's ui module."""
    ui = load(cfg).ui
    return ui.feature_menu(result), ui.part_menu(result)


def conflict_menu(cfg: dict, err) -> list:
    return load(cfg).ui.conflict_menu(err)
