# Setting up Discord for the network

About 15 minutes. You create **one server**, **three bot applications** and **three channels**.

## 1. A private server and three channels

1. In Discord: **+ → Create My Own → For me and my friends**. Keep it invite-only.
2. Create text channels `#projects`, `#prototypes`, `#enclosures` (names are only a convention).
3. Settings → **Advanced → Developer Mode: on**. Now right-click each channel → **Copy Channel ID** and keep the three numbers.
4. Also copy your own user ID (right-click your name → **Copy User ID**). MegaMan uses it as `owner_user_id`.

## 2. One application per bot

A bot token identifies exactly one bot, so create three applications at <https://discord.com/developers/applications> → **New Application**: `MegaMan`, `Protoman`, `Gutsman` (any names work).

For each one:

1. **Bot** tab → **Reset Token** → copy it somewhere safe. You will paste it once into the bot's setup. Treat it like a password.
2. Same tab → **Privileged Gateway Intents** → enable **Message Content Intent**. The wizards read your plain-text answers; without this the bot sees nothing you type.
3. Optional: upload an avatar (General Information → App Icon, and the Bot tab).
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; permissions **View Channels**, **Send Messages**, **Attach Files**, **Embed Links**, **Use Slash Commands**. Open the generated URL and add the bot to your server.
   (Protoman can print this link for you: `python3 protoman.py --invite`.)

## 3. Limit each bot to its channel

The bots enforce their own channel, so this is belt-and-braces, but do it anyway:

- Server Settings → Roles → make each bot's role; on `#projects` deny the other two bots **View Channel**, and so on.
- Keep MegaMan's channel visible only to you.

## 4. Give each bot its token and channel ID

| Bot | How | Fields |
|---|---|---|
| MegaMan | `cd megaman && python3 claude_pm.py --setup` | token, `#projects` channel ID, projects folder |
| Protoman | `cd protoman && cp .env.example .env`, edit | `DISCORD_TOKEN`, `PROTOMAN_CHANNEL_ID` |
| Gutsman | `cd gutsman && python3 gutsman.py --setup` | token, `#enclosures` channel ID |

MegaMan also records `owner_user_id` from your first command if you leave it empty; set it explicitly so nobody else in the server can drive your machine.

## 5. Check it

- Start a bot in the foreground and look in the log for the line naming your server and channel. Slash commands appear within seconds in your server (they are registered per server, not globally).
- In `#enclosures` type `/boxhelp`; in `#projects` type `/status`; for Protoman run `python3 protoman.py --check` first.
- Nothing appears? The usual causes: Message Content Intent is off, the bot was invited without `applications.commands`, or you're typing in a channel other than the one you configured.

## 6. Run them as services

See each bot's README; all three have a systemd user unit, and Protoman a Docker option. Run `loginctl enable-linger "$USER"` so user services survive logout.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "The application did not respond" | The bot is down: `systemctl --user status <unit>`, `journalctl --user -u <unit> -f` |
| Bot online, ignores plain messages | Message Content Intent off (step 2.2) |
| Slash commands missing | Re-invite with `applications.commands`; restart the bot |
| "Only works in its own channel" | Wrong channel ID in config |
| Leaked a token | Developer Portal → Bot → Reset Token; update the config; restart |
