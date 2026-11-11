#!/usr/bin/env python3
"""Run MegaMan's real command handlers against a throw-away projects folder and
save what he would say, plus the project files `/new` scaffolds.

    record_megaman.py <out_dir>

No Discord, no tmux, no systemd: process lookups are patched to a fixed demo
state, everything else is MegaMan's own code. Writes <out_dir>/transcript.json
and <out_dir>/scaffold/<project>/ (the files /new would create).
"""
import asyncio, json, logging, shutil, sys, tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent / "megaman"
sys.path.insert(0, str(ROOT))
import claude_pm as cp                                  # noqa: E402

out = Path(sys.argv[1]).resolve(); out.mkdir(parents=True, exist_ok=True)
root = Path(tempfile.mkdtemp(prefix="mm-projects-"))
for n in ("bracket-fixture", "weather-dash", "ctf-warmup", "gb-platformer"):
    (root / n).mkdir()
(root / "weather-dash" / "launch.sh").write_text("#!/bin/bash\n")
cfg = dict(cp.DEFAULT_CONFIG, projects_root=str(root), session_mem_high="6G",
           session_mem_max="10G", discord_token="", discord_channel_id="")
log = logging.getLogger("rec")
h = cp.CommandHandler(cfg, log)
tr = []
def say(who, text): tr.append({"who": who, "text": text, "images": []})

# /list, with a fixed demo state
say("user", "/list")
with patch.object(cp, "active_sessions", return_value=["cp-weather-dash", "cp-bracket-fixture", "cp-boxbot"]), \
     patch.object(cp, "active_jobs", return_value=["job-weather-dash"]), \
     patch.object(cp, "session_uptimes", return_value={"cp-weather-dash": 7500, "cp-bracket-fixture": 1260,
                                                       "cp-boxbot": 600, "job-weather-dash": 3400}), \
     patch.object(cp, "session_idle_sec", return_value=None), \
     patch.object(cp, "launch_port", return_value=8100), \
     patch.object(cp, "health_ok", return_value=True):
    say("bot", h._list())

say("user", "/help")
say("bot", h._help())

# /new: the conversational wizard (the slash card drives the same data)
wiz = cp.ProjectWizard(cfg, log)
say("user", "/new")
say("bot", wiz.start())
for a in ("garden-monitor", "A small dashboard that shows soil moisture from my garden sensors", "1", "fastapi",
          "Read the sensor JSON, chart 24h of moisture, no login", "-"):
    say("user", a)
    say("bot", asyncio.run(wiz.step(a)))

# the files it scaffolds
(root / wiz.data["name"]).mkdir()       # _execute() makes the folder; the rest is write_project_files
with patch.object(cp, "load_config", return_value=cfg):
    cp.write_project_files(cfg, wiz.data)
dest = out / "scaffold"
shutil.rmtree(dest, ignore_errors=True)
shutil.copytree(root / "garden-monitor", dest / "garden-monitor", ignore=shutil.ignore_patterns(".git"))
for m in tr: m["text"] = m["text"].replace(str(root), "~/ai-projects")
(out / "transcript.json").write_text(json.dumps(tr, indent=1, ensure_ascii=False))
print(f"{len(tr)} messages")
for m in tr: print(f"[{m['who']}] {m['text'][:100]!r}")
print(sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()))
