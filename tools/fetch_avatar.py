#!/usr/bin/env python3
"""Download a bot's own avatar. Reads the token from the bot's config file and
never prints or stores it.   fetch_avatar.py <config.json> <out.png>"""
import json, sys, urllib.request
cfg, out = sys.argv[1], sys.argv[2]
tok = json.load(open(cfg))["discord_token"]
def get(url, auth=False):
    h = {"User-Agent": "DiscordBot (avatar-fetch, 1.0)"}
    if auth: h["Authorization"] = "Bot " + tok
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20).read()
me = json.loads(get("https://discord.com/api/v10/users/@me", True))
if not me.get("avatar"): sys.exit("bot has no custom avatar")
open(out, "wb").write(get(f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=256"))
print("saved", out, "for", me["username"])
