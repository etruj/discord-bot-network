"""Part folders under parts_root, and the short keys Discord buttons carry.

Each part is a *family* of version folders, the engine's own convention:

    parts_root/20261027_141500_bracket/        v1 (the "family key")
    parts_root/20261027_141500_bracket_v2/     v2, v3, ... (made by revisions)

Buttons and dropdowns carry the family key in their custom_id, never a
version, so a button on an older message always acts on the newest version.
That's also what keeps them working after a bot restart: nothing about a part
lives only in memory.
"""
import json
import re
import time
from pathlib import Path

KEY_RE = re.compile(r"^\d{8}_\d{6}_[a-z0-9_]{1,24}$")
VER_RE = re.compile(r"_v(\d+)$")
MAX_NAME = 24


def slug(text: str, fallback: str = "part") -> str:
    """Folder-safe part name. Never ends in _v<digits>, which the engine
    would read as a version suffix."""
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:MAX_NAME].strip("_")
    s = s or fallback
    if VER_RE.search(s):
        s = s[:MAX_NAME - 2] + "_p"
    return s


def new_job_dir(root: Path, name: str) -> Path:
    """A fresh v1 folder: <timestamp>_<slug>. Waits out a same-second clash."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    while True:
        d = root / f"{time.strftime('%Y%m%d_%H%M%S')}_{slug(name)}"
        if not d.exists():
            d.mkdir()
            return d
        time.sleep(1)


def family_key(job_dir) -> str:
    return VER_RE.sub("", Path(job_dir).name)


def versions(root: Path, key: str) -> dict:
    """{version: folder} for finished versions (they have a result.json)."""
    if not KEY_RE.match(key or ""):
        return {}
    root = Path(root)
    out = {}
    for d in [root / key, *root.glob(f"{key}_v*")]:
        m = re.fullmatch(re.escape(key) + r"(?:_v(\d+))?", d.name)
        if m and (d / "result.json").is_file():
            out[int(m.group(1) or 1)] = d
    return out


def latest(root: Path, key: str):
    """Newest finished version folder of a family, or None."""
    v = versions(root, key)
    return v[max(v)] if v else None


def families(root: Path) -> list:
    """Family keys with at least one finished version, newest first."""
    root = Path(root)
    if not root.is_dir():
        return []
    keys = {family_key(d) for d in root.iterdir()
            if d.is_dir() and (d / "result.json").is_file()}
    return sorted((k for k in keys if KEY_RE.match(k)), reverse=True)


def pretty(key: str) -> str:
    """'20261027_141500_bracket' → 'bracket (Oct 27 14:15)'."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})\d{2}_(.+)", key)
    if not m:
        return key
    y, mo, d, h, mi, name = m.groups()
    month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[int(mo) - 1]
    return f"{name} ({month} {int(d)} {h}:{mi})"


class LastPart:
    """Which part each user touched last, so a typed edit with no reply
    target goes somewhere sensible. Kept on disk so it survives restarts."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, user_id) -> str:
        return self._load().get(str(user_id), "")

    def set(self, user_id, key: str):
        data = self._load()
        data[str(user_id)] = key
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path)
        except OSError:
            pass  # best effort: losing it only means "reply to the card"
