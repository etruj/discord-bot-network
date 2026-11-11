#!/usr/bin/env python3
"""Render a recorded transcript as a Discord-style chat image (Chromium via Playwright).

    render_chat.py <transcript.json> <out.png> --bot NAME [--avatar FILE] [--range A:B] [--width 760]

The transcript comes from record_megaman.py / record_gutsman.py: a list of
{"who": "user"|"bot", "text": ..., "images": [files next to the transcript]}.
A user message that starts with "/" is drawn as a slash-command invocation.
The styling only approximates Discord; the text and pictures are the bot's own.
"""
import argparse, asyncio, html, json, re
from pathlib import Path
from playwright.async_api import async_playwright

ap = argparse.ArgumentParser()
ap.add_argument("transcript"); ap.add_argument("out")
ap.add_argument("--bot", required=True); ap.add_argument("--avatar")
ap.add_argument("--range", default=":"); ap.add_argument("--width", type=int, default=760)
ap.add_argument("--user", default="maker"); ap.add_argument("--color", default="#c0392b")
ap.add_argument("--stamp", default="Today at 9:41 AM")
a = ap.parse_args()

src = Path(a.transcript).resolve()
msgs = json.load(open(src))
lo, _, hi = a.range.partition(":")
msgs = msgs[int(lo or 0): int(hi) if hi else None]

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#313338;color:#dbdee1;font:15px/1.4 'Noto Sans','DejaVu Sans',sans-serif;width:%dpx;padding:14px 0}
.m{display:flex;gap:14px;padding:5px 16px}
.av{flex:none;width:40px;height:40px;border-radius:50%%;display:flex;align-items:center;justify-content:center;font-size:19px;font-weight:700;color:#fff;overflow:hidden}
.av img{width:100%%;height:100%%;object-fit:cover}
.u{background:#5865f2}.b{background:%s}.av.light{background:#f2f2f2}
.b0{min-width:0;flex:1}
.h{display:flex;align-items:center;gap:7px}
.n{font-weight:600;color:#f2f3f5}.n.bot{color:#f2f3f5}
.tag{background:#5865f2;color:#fff;font-size:10px;font-weight:700;padding:0 4px;border-radius:3px;line-height:15px}
.t{font-size:12px;color:#949ba4}
.ctx{font-size:13px;color:#b5bac1;margin-left:54px;padding:6px 16px 0}
.ctx b{color:#f2f3f5;font-weight:500}.ctx .cmd{color:#c9cdfb}
.c{overflow-wrap:anywhere}
.c p{margin:0 0 4px;white-space:pre-wrap}
code{background:#1e1f22;border-radius:3px;padding:1px 4px;font:13px 'DejaVu Sans Mono',monospace}
pre{background:#2b2d31;border:1px solid #1e1f22;border-radius:6px;padding:8px 10px;margin:4px 0;font:12.5px/1.35 'DejaVu Sans Mono',monospace;white-space:pre-wrap;overflow-wrap:anywhere}
pre code{background:none;padding:0;font:inherit}
ul{margin:2px 0 4px;padding-left:22px}
.g{display:grid;gap:4px;margin-top:6px;max-width:560px}
.g.n2{grid-template-columns:1fr 1fr}
.g img{width:100%%;border-radius:6px;display:block;background:#fff}
""" % (a.width, a.color)

def inline(t):
    t = html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<i>\1</i>", t)
    return t

def md(text):
    out, parts = [], re.split(r"```[a-z]*\n(.*?)```", text, flags=re.S)
    for i, p in enumerate(parts):
        if i % 2:
            out.append(f"<pre><code>{html.escape(p.rstrip())}</code></pre>")
            continue
        bullets = []
        for line in p.strip("\n").split("\n"):
            if re.match(r"^\s*[•\-] ", line):
                bullets.append(inline(re.sub(r"^\s*[•\-] ", "", line))); continue
            if bullets:
                out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>"); bullets = []
            out.append(f"<p>{inline(line)}</p>" if line.strip() else '<div style="height:6px"></div>')
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
    return "".join(out)

if a.avatar:
    av = f'<div class="av b"><img src="{Path(a.avatar).resolve().as_uri()}"></div>'
else:
    av = f'<div class="av b">{html.escape(a.bot[0])}</div>'
uav = '<div class="av u">m</div>'

body = []
for i, m in enumerate(msgs):
    text = m["text"]
    if m["who"] == "user" and text.startswith("/"):
        body.append(f'<div class="ctx"><b>{a.user}</b> used <span class="cmd">{html.escape(text)}</span></div>')
        continue
    pics = "".join(f'<img src="{(src.parent / n).as_uri()}">' for n in m["images"])
    grid = f'<div class="g n{min(len(m["images"]),2)}">{pics}</div>' if pics else ""
    if m["who"] == "bot":
        head = f'<span class="n bot">{html.escape(a.bot)}</span><span class="tag">APP</span>'
        icon = av
    else:
        head = f'<span class="n">{a.user}</span>'
        icon = uav
    body.append(f'<div class="m">{icon}<div class="b0"><div class="h">{head}<span class="t">{a.stamp}</span></div>'
                f'<div class="c">{md(text)}</div>{grid}</div></div>')

page = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(body)}</body></html>"

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": a.width, "height": 400}, device_scale_factor=2)
        tmp = src.parent / ".render_tmp.html"
        tmp.write_text(page)
        await pg.goto(tmp.as_uri()); await pg.wait_for_timeout(400)
        tmp.unlink()
        await pg.screenshot(path=a.out, full_page=True)
        await b.close()
asyncio.run(main())
print("wrote", a.out)
