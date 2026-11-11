#!/usr/bin/env python3
"""Drive one of Gutsman's real wizards with scripted answers and save the transcript.

    record_gutsman.py <rugged|sliding|project|circular> <out_dir> <answer> [<answer> ...]

Runs the real wizard (and OpenSCAD) locally. Writes <out_dir>/transcript.json:
[{"who": "user"|"bot", "text": str, "images": [filenames]}], with images copied
into <out_dir>. Nothing here touches Discord or any token.
"""
import asyncio, json, logging, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "gutsman"
sys.path.insert(0, str(ROOT))
from box_types import by_command            # noqa: E402

kind, out = sys.argv[1], Path(sys.argv[2]).resolve()
answers = sys.argv[3:]
out.mkdir(parents=True, exist_ok=True)
boxes = Path(tempfile.mkdtemp(prefix="gutsman-boxes-"))
wiz = by_command(kind).Wizard({"boxes_root": str(boxes)}, logging.getLogger("rec"))
log = []

def bot(r):
    text, imgs = (r if isinstance(r, tuple) else (r, []))
    names = []
    for p in imgs or []:
        p = Path(p)
        dest = out / f"{kind}-{len(log):02d}-{p.name}"
        shutil.copy(p, dest)
        names.append(dest.name)
    log.append({"who": "bot", "text": text, "images": names})

async def main():
    bot(wiz.start())
    for a in answers:
        log.append({"who": "user", "text": a, "images": []})
        bot(await wiz.step(a))
asyncio.run(main())
for m in log:   # show a tidy path in the screenshots instead of the temp folder
    m["text"] = m["text"].replace(str(boxes), "~/ai-projects/boxes")
(out / "transcript.json").write_text(json.dumps(log, indent=1, ensure_ascii=False))
print(f"{len(log)} messages -> {out}")
for m in log:
    print(f"[{m['who']}] {m['text'][:110]!r} {m['images']}")
