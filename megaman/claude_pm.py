#!/usr/bin/env python3
"""
claude_pm.py — Claude Project Manager (Discord bot)

Manages Claude Code remote-control sessions from your phone. Native slash
commands with project-name autocomplete. Runs as a systemd user service.

Slash commands (29):
  /new /box /cancel /open /revive /refresh /debug /list /log /stop /url /delete
  /run /halt /jobs /output /render /screenshot
  /status /health /logs /usage /help
  /relogin /logincode /selfupdate /reboot /sudo /mem

Setup:
  1. pip3 install --user discord.py
  2. python3 claude_pm.py --setup
  3. python3 claude_pm.py   (or install claude-pm.service)
"""

import os
import re
import getpass
import sys
import math
import uuid
import json
import shutil
import socket
import asyncio
import tempfile
import logging
import logging.handlers
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── Configuration ─────────────────────────────────────────────────────────────

CONFIG_FILE  = Path.home() / ".claude_pm.json"
TEMPLATE_DIR = Path(__file__).parent / "templates"

DEFAULT_CONFIG = {
    "projects_root":       str(Path.home() / "ai-projects"),
    "discord_token":       "",
    "discord_channel_id":  "",
    "log_file":            str(Path.home() / ".claude_pm.log"),
    "tmux_prefix":         "cp",
    # Next port to hand out to a new web project (increments per project so
    # concurrently running jobs never collide).
    "next_web_port": 8100,
    # Headless Game Boy screenshot command for /screenshot. Template with
    # {rom} and {out} placeholders; must write a PNG to {out} and exit 0.
    # Empty = /screenshot explains how to set this up.
    "gb_screenshot_cmd": "",
    # Auto-purge soft-deleted projects from .trash/ older than this many days.
    # 0 = never auto-purge.
    "trash_retention_days": 20,
    # On /revive, attach a "where we left off" recap built from the newest
    # LOG.md entry + the last request/reply (no model call). false = skip.
    "revive_summary":      True,
    # Usage watchdog: @-mention the owner when usage crosses these thresholds.
    # Needs the matching caps (usage_window_limit / usage_weekly_limit) set.
    "owner_user_id":        "",     # auto-filled from your first command if empty
    "usage_alert_5h_pct":   85,     # alert when 5-hour usage hits this %
    "usage_alert_weekly_pct": 90,   # alert when weekly usage hits this %
    "usage_alert_interval_sec": 1800,  # how often the watchdog checks
    # Per-session memory guardrail. Each Claude session runs inside its own
    # systemd --user scope with these caps, so a runaway (e.g. a huge mesh
    # boolean) is killed alone instead of OOM-ing the whole laptop and taking
    # every other session down with it. Set session_mem_max to "" to disable.
    # The soft cap is the *block*: past it the kernel throttles, the watchdog
    # warns, and the session is expected to shed load or ask for a lift
    # (claude-memcheck --request → /mem action:grant). The hard cap sits well
    # above it so that asking is still possible — it only exists to stop a
    # runaway from taking the laptop down.
    "session_mem_high":      "6G",  # soft cap: throttle + reclaim at this RSS
    "session_mem_max":      "10G",  # hard cap: kill the session's tree here
    "session_mem_swap_max":  "2G",  # cap swap so it can't thrash for hours first
    # Shared ceiling over ALL sessions + jobs together (one systemd slice). The
    # per-session caps alone summed past the laptop's your RAM: two heavy
    # sessions could OOM the machine while each stayed under its own 8G.
    # Leaves ~5 GB for the desktop, browser and this bot. "" disables.
    "sessions_total_mem_high": "10G",
    "sessions_total_mem_max":  "12G",
    "sessions_total_swap_max": "3G",
    # Ceiling on what `/mem action:grant` may hand out (per session, and for
    # the shared slice). Grants above it are refused with the number the owner would
    # have to raise it to — `/mem action:ceiling size:<N>` does that. your RAM
    # laptop, so this leaves ~2.5 GB for the desktop, browser and this bot.
    "mem_grant_ceiling":     "13G",
    # Memory watchdog: warn when a session's working set reaches this % of its
    # SOFT cap (where throttling starts), or when it stalls on memory (PSI
    # some avg60 ≥ session_mem_alert_psi %) — i.e. already thrashing.
    "session_mem_alert_soft_pct": 90,
    "session_mem_alert_psi":      10,
    # Idle-session watchdog. A session parked with no conversation activity is
    # burning ~1GB of RAM for nothing, and once it sits past the ~8h OAuth token
    # life it can wake up unable to re-auth ("Please run /login", which
    # remote-control can't do) — so a stale session is better revived than
    # resumed. Set session_idle_alert_hours to 0 to disable.
    "session_idle_alert_hours":   12,    # warn once a session is idle this long
    "session_idle_interval_sec":  1800,  # how often the watchdog checks
    # Power watchdog: @-mention the owner when the laptop's power cord comes
    # out (it runs every session — a dead battery takes them all down), again
    # as the charge falls to each of power_alert_battery_pcts, and an all-clear
    # when it's plugged back in. Reads /sys/class/power_supply.
    "power_alert":              True,
    "power_check_interval_sec": 15,
    "power_alert_battery_pcts": [50, 25, 10],
}

# Project types offered by /new, as (menu label, lang, framework, test cmd,
# run cmd). `lang` is the key everything downstream branches on.
#
# Condensed 2026-10-20: the menu used to carry five separate web/Node entries
# (FastAPI, Flask, Django, Express, Next.js) plus two "bare" ones. They all
# produced the same shape of project, so they are now one "Web app / API" row
# whose follow-up question picks the runtime — while the specialties the owner
# actually lives in (OpenSCAD, RE, both halves of CTF, Game Boy, KiCad) each
# keep their own row and their own scaffold.
STACKS = {
    "1": ("Web app / API",             "Web",      "", "",                      ""),
    "2": ("Script / CLI tool",         "Script",   "", "",                      ""),
    "3": ("Systems / native",          "Native",   "", "",                      ""),
    "4": ("Reverse engineering (REA)", "RE",       "", "",                      ""),
    "5": ("CTF — solve a challenge",   "CTF",      "", "",                      ""),
    "6": ("CTF — build a challenge",   "CTFDEV",   "", "",                      ""),
    "7": ("Game development (Game Boy)", "GBDK",   "", "make (ROM builds clean)", "make"),
    "8": ("OpenSCAD (3D print)",       "OpenSCAD", "", "",                      "openscad"),
    "9": ("KiCad (PCB design)",        "KiCad",    "", "ERC + DRC clean",       "kicad"),
    "0": ("Bare / other",              "",         "", "",                      ""),
}

# Runtime behind the three condensed rows. The wizard asks one free-text
# question ("which language/framework?") and this resolves the answer to the
# concrete stack the old menu rows used to hard-code. First match wins, so the
# specific frameworks are listed before the bare languages.
_RUNTIME_HINTS = [
    (("fastapi",),                  "Python",     "FastAPI", "pytest",        "uvicorn main:app --reload"),
    (("flask",),                    "Python",     "Flask",   "pytest",        "flask run"),
    (("django",),                   "Python",     "Django",  "pytest",        "python manage.py runserver"),
    (("nextjs", "next"),            "JavaScript", "Next.js", "jest",          "npm run dev"),
    (("express",),                  "JavaScript", "Express", "jest",          "npm run dev"),
    (("react", "vite", "svelte", "vue"),
                                    "JavaScript", "Vite",    "vitest",        "npm run dev"),
    (("typescript", "node", "nodejs", "javascript", "js", "ts"),
                                    "JavaScript", "",        "jest",          "node index.js"),
    (("python", "py"),              "Python",     "",        "pytest",        "python main.py"),
    (("rust", "cargo"),             "Rust",       "",        "cargo test",    "cargo run"),
    (("golang", "go"),              "Go",         "",        "go test ./...", "go run ."),
    (("cpp", "c++", "cmake", "c"),  "C/C++",      "",        "ctest",         "./build/app"),
    (("bash", "shell", "sh"),       "Bash",       "",        "bats",          "./main.sh"),
]

# What a condensed row falls back to when the follow-up answer is skipped or
# unrecognised — the stack the owner reaches for most in that shape of project.
_GROUP_DEFAULT = {
    "Web":    ("Python",     "FastAPI", "pytest",     "uvicorn main:app --reload"),
    "Script": ("Python",     "",        "pytest",     "python main.py"),
    "Native": ("Rust",       "",        "cargo test", "cargo run"),
}

def resolve_runtime(group: str, detail: str) -> tuple:
    """(lang, framework, test_fw, run_cmd) for a condensed row + its answer."""
    norm = re.sub(r"[^a-z0-9+]+", " ", (detail or "").lower())
    words = set(norm.split())
    for keys, lang, fw, test_fw, run_cmd in _RUNTIME_HINTS:
        if any(k in words for k in keys):
            return lang, fw, test_fw, run_cmd
    return _GROUP_DEFAULT.get(group, ("", "", "", ""))

# Launch type per lang, inferred instead of asked (the wizard used to spend two
# of its nine steps on this and on sudo). Everything else gets "none".
LAUNCH_BY_LANG = {"Web": "web", "Script": "cli", "Native": "cli",
                  "RE": "cli", "GBDK": "cli"}

def launch_type_for(lang: str) -> str:
    return LAUNCH_BY_LANG.get(lang, "none")


# Per-stack scaffold bundles, keyed by language. Convention: the directory
# holds a SKILL.md (prepended into the project's SKILLS.md) plus any other
# files/dirs, which are copied verbatim into the project root. Adding a new
# stack bundle needs no code change beyond this dict.
# Generated boxes live under projects_root/<BOXES_DIRNAME>/<box>. It's not a
# project, so /list, /open and the autocompletes skip it.
BOXES_DIRNAME = "boxes"
# claude-pm / boxbot are the two bots' /debug sessions (cp-claude-pm,
# cp-boxbot); a project with either name would share their tmux session.
RESERVED_NAMES = {"claude-pm", "boxbot", BOXES_DIRNAME}

_ASSETS = Path(__file__).parent / "scaffolds"
STACK_SCAFFOLDS = {
    "OpenSCAD": _ASSETS / "3d-modeling" / "printable-parametric-parts",
    "GBDK":     _ASSETS / "gbdk" / "gameboy-dev",
    "KiCad":    _ASSETS / "kicad" / "pcb-design",
    "RE":       _ASSETS / "re" / "reverse-engineering",
    "CTFDEV":   _ASSETS / "ctf" / "challenge-authoring",
}

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("claude_pm")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    # Rotate at 5 MB (3 backups) — unbounded growth would eventually make
    # /logs slow and eat disk on a machine nobody is watching.
    file_h = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3)
    for h in [file_h, logging.StreamHandler()]:
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()

def _atomic_write(path: Path, text: str):
    """Write via tmp + rename so a crash mid-write can't corrupt the file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)

def save_config(cfg: dict):
    _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2))

# ── Workspace trust ──────────────────────────────────────────────────────────

CLAUDE_JSON = Path.home() / ".claude.json"

# ── REA harness (reverse-engineering projects) ──────────────────────────────
# REA is one MCP server with ~200 tools, so it is registered per project
# (local scope: ~/.claude.json → projects[<dir>].mcpServers) rather than
# globally — only RE sessions pay for it, and they need no approval prompt.
# Providers are passed in its env: REA refuses Ghidra < 12.1 and JDK < 21,
# while an older /opt/ghidra or /opt/jdk-17 may belong to another project and
# must stay where they are.
REA_BINS = [Path.home() / ".local" / "bin" / "rea", Path("/usr/local/bin/rea"),
            Path("/usr/bin/rea")]
REA_PKG  = "rea-agents@latest"
# Caller-supplied engine REA needs for Android decompilation (pinned 0.7.1,
# sha256 6e5eacf5…ff75). See tools/install-rea-android.sh.
JADX_MCP_JAR = Path.home() / "tools" / "rea-android" / "jadx-headless-mcp-0.7.1-all.jar"

def _newest_ghidra():
    """Newest Ghidra 12.1+ install REA will accept, or None."""
    cands = []
    for root in (Path.home() / "tools", Path("/opt"), Path.home()):
        try:
            for d in root.glob("ghidra*"):
                for g in ([d] + list(d.glob("ghidra_*_PUBLIC"))):
                    props = g / "Ghidra" / "application.properties"
                    if not props.is_file():
                        continue
                    m = re.search(r"application\.version=([\d.]+)", props.read_text())
                    if m:
                        ver = tuple(int(x) for x in m.group(1).split(".")[:3])
                        cands.append((ver, g))
        except OSError:
            continue
    ok = [c for c in cands if c[0] >= (12, 1)]
    return max(ok)[1] if ok else None

def _jdk21_plus():
    """A JDK home for REA's Ghidra provider, or None.

    Lowest JDK >= 21: that is Ghidra 12's documented baseline, and the newest
    JDK on the box is the one most likely to be ahead of what it was tested
    against. (REA refuses anything older than 21.)
    """
    best = None
    jvm = Path("/usr/lib/jvm")
    for d in sorted(jvm.glob("java-*-openjdk*")) if jvm.is_dir() else []:
        m = re.search(r"java-(\d+)", d.name)
        if m and int(m.group(1)) >= 21 and (d / "bin" / "java").exists():
            if best is None or int(m.group(1)) < best[0]:
                best = (int(m.group(1)), d)
    return best[1] if best else None

def rea_mcp_server() -> dict:
    """The `rea` stdio MCP server entry to register for an RE project."""
    binary = next((b for b in REA_BINS if b.exists()), None)
    cmd, args = ((str(binary), ["mcp"]) if binary
                 else ("npx", ["-y", REA_PKG, "mcp"]))
    env = {}
    gh = _newest_ghidra()
    if gh:
        env["GHIDRA_INSTALL_DIR"] = str(gh)
    jdk = _jdk21_plus()
    if jdk:
        env["JAVA_HOME"] = str(jdk)
    # Android. REA never installs these; it only finds what it is pointed at.
    # Each is added only if present, so a missing tool shows up as REA's own
    # "not configured" remediation instead of a path to nothing.
    for var, cands in (
            ("REA_APKTOOL_COMMAND", ["/usr/local/bin/apktool", "/usr/bin/apktool"]),
            ("REA_ADB_PATH", ["/opt/android-sdk/platform-tools/adb",
                              "/usr/local/bin/adb", "/usr/bin/adb"]),
            ("REA_JADX_MCP_JAR", [str(JADX_MCP_JAR)])):
        found = next((c for c in cands if Path(c).is_file()), None)
        if found:
            env[var] = found
    if "REA_JADX_MCP_JAR" in env:
        # JADX's heap is the thing that grows with the APK, and a hard cap kills
        # the whole session — so bound it explicitly, under the 6G soft cap.
        env["REA_JADX_HEAP_MIB"] = "3072"
    return {"type": "stdio", "command": cmd, "args": args, "env": env}

def trust_workspace(project_path: str, logger: logging.Logger = None,
                    mcp_servers: dict = None):
    """Add a trusted-workspace entry to ~/.claude.json so remote-control works.

    `mcp_servers` (optional) registers project-local MCP servers in the same
    write — Claude Code's "local" scope, which needs no approval prompt.
    """
    data = {}
    if CLAUDE_JSON.exists():
        try:
            data = json.loads(CLAUDE_JSON.read_text())
        except (json.JSONDecodeError, OSError) as e:
            if logger:
                logger.warning(f"Could not read {CLAUDE_JSON}: {e}")
    projects = data.setdefault("projects", {})
    if project_path not in projects:
        projects[project_path] = {}
    projects[project_path]["hasTrustDialogAccepted"] = True
    if mcp_servers:
        projects[project_path].setdefault("mcpServers", {}).update(mcp_servers)
    try:
        # Atomic: a partial write here would corrupt Claude Code's global
        # config and break every session on the machine.
        _atomic_write(CLAUDE_JSON, json.dumps(data, indent=2))
        if logger:
            logger.info(f"Trusted workspace: {project_path}")
    except OSError as e:
        if logger:
            logger.error(f"Failed to write {CLAUDE_JSON}: {e}")

# ── Claude account info ───────────────────────────────────────────────────────

def claude_plan_info() -> dict:
    """Read plan/tier from ~/.claude/.credentials.json (no live usage there)."""
    cred = Path.home() / ".claude" / ".credentials.json"
    out = {"plan": "", "tier": ""}
    try:
        oauth = json.loads(cred.read_text()).get("claudeAiOauth", {})
        out["plan"] = (oauth.get("subscriptionType") or "").strip()
        out["tier"] = (oauth.get("rateLimitTier") or "").strip()
    except (OSError, json.JSONDecodeError):
        pass
    return out

# ── Live rate-limit probe (clawdmeter-daemon technique) ──────────────────────
# A minimal 1-token API call returns Anthropic's *authoritative* unified
# rate-limit headers — the same 5h/7d percentages claude.ai shows — replacing
# what an earlier local-JSONL estimate approximated (removed 2026-10-15).

_probe_cache = {"at": 0.0, "data": None, "last_err": None}
PROBE_CACHE_SEC = 60
# Models the probe tries, in order (it asks for 1 output token; it only exists
# to read the rate-limit headers). Measured 2026-10-15: through this bare OAuth
# call, every 5-generation model (haiku-5-5, sonnet-5-5, opus-5, fable-5-1)
# answers 429 with NO rate-limit headers. Claude Code's own client gets through,
# a raw request does not. claude-haiku-4-5 answers 200 with headers. Use the
# alias, not a dated snapshot. The rest are fallbacks for when 4.5 retires:
# on a 404/header-less reply the next one is tried, and the first that works is
# remembered. /health reports probe failures, so this can't rot silently.
PROBE_MODELS = ("claude-haiku-4-5", "claude-haiku-5-5", "claude-sonnet-5-5")

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
# Refresh this many seconds before the token actually expires, so a probe never
# races the expiry boundary.
TOKEN_SKEW_SEC = 300
# Refresh this far ahead of expiry when we *proactively* decide to renew. Kept
# larger than the watchdog's probe interval (usage_alert_interval_sec, 1800s) so
# a full probe cycle always lands before expiry — the on-disk token therefore
# stays continuously valid, which is what lets an idle `claude remote-control`
# session re-auth from the file instead of dead-ending at "Please run /login".
REFRESH_AHEAD_SEC = 2400
# Claude Code's public OAuth client + token endpoint, lifted verbatim from the
# vendor CLI binary (bin/claude.exe). Using the vendor's own client_id keeps our
# refresh_token grant identical to what the CLI itself issues.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# platform.claude.com sits behind Cloudflare, which blocks the default
# `Python-urllib/x` User-Agent outright (HTTP 403, "error code: 1010") before
# the request ever reaches the OAuth handler. Any CLI-shaped UA passes; the
# version is cosmetic to Cloudflare, so a static one is fine.
OAUTH_USER_AGENT = "claude-cli/2.1.195 (external, cli)"

def _token_expires_at() -> float:
    """Unix seconds at which the stored OAuth token expires, 0.0 if unknown."""
    try:
        creds = json.loads(CREDENTIALS_FILE.read_text())
        exp = creds.get("claudeAiOauth", creds).get("expiresAt")
        return float(exp) / 1000 if exp else 0.0  # stored in milliseconds
    except (OSError, ValueError, TypeError):
        return 0.0

def _persist_credentials(new_tokens: dict, logger: logging.Logger = None) -> str:
    """Merge a fresh token grant into ~/.claude/.credentials.json (caller holds
    the lock). Returns the new access token, or "" on write failure.

    Re-reads the file *under the lock* so we merge onto whatever the vendor CLI
    last wrote (preserving every field it stores), backs it up once, and swaps
    it in atomically (temp file + os.replace, 0600) so a crash mid-write can
    never leave the CLI's credential file truncated.
    """
    access = new_tokens.get("access_token")
    if not access:
        return ""
    try:
        cur = json.loads(CREDENTIALS_FILE.read_text())
    except (OSError, ValueError):
        cur = {"claudeAiOauth": {}}
    nested = "claudeAiOauth" in cur
    oauth = cur.get("claudeAiOauth", cur)
    oauth["accessToken"] = access
    if new_tokens.get("refresh_token"):
        oauth["refreshToken"] = new_tokens["refresh_token"]
    if new_tokens.get("expires_in"):
        oauth["expiresAt"] = int((_now_sec() + int(new_tokens["expires_in"])) * 1000)
    if nested:
        cur["claudeAiOauth"] = oauth
    else:
        cur = oauth
    try:
        shutil.copy2(CREDENTIALS_FILE, CREDENTIALS_FILE.with_suffix(".json.bak"))
    except OSError:
        pass  # backup is best-effort; never block the refresh on it
    fd, tmp = tempfile.mkstemp(dir=str(CREDENTIALS_FILE.parent),
                               prefix=".cred-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cur, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CREDENTIALS_FILE)
    except OSError as e:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if logger:
            logger.warning(f"OAuth credential write failed: {e}")
        return ""
    return access

def _oauth_refresh_grant(logger: logging.Logger = None) -> str:
    """Renew the OAuth access token via the refresh_token grant, or return "".

    Serialised under an exclusive flock on a sibling lock file so two bot code
    paths (or two ticks) never refresh — and thus never rotate the single-use
    refresh token — at the same time. Under the lock we re-check expiry first:
    if another refresher or a fresh CLI login already renewed the token while we
    waited, we skip the network call entirely and just return the on-disk token.
    That "only refresh when truly needed" discipline keeps refresh-token
    rotation to a minimum, which is what avoids yanking the token out from under
    a still-running `claude` session.
    """
    import fcntl
    import urllib.request
    lock_fd = os.open(str(CREDENTIALS_FILE.with_suffix(".json.lock")),
                      os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if _token_expires_at() - _now_sec() > TOKEN_SKEW_SEC:
            return _read_token_file()  # someone else already refreshed
        try:
            oauth = json.loads(CREDENTIALS_FILE.read_text())
        except (OSError, ValueError):
            return ""
        refresh = oauth.get("claudeAiOauth", oauth).get("refreshToken")
        if not refresh:
            return ""
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": OAUTH_CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            OAUTH_TOKEN_URL, data=body, method="POST",
            headers={"content-type": "application/json",
                     "User-Agent": OAUTH_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                grant = json.loads(r.read().decode())
        except Exception as e:
            if logger:
                logger.warning(f"OAuth refresh grant failed: {e}")
            return ""
        tok = _persist_credentials(grant, logger)
        if tok and logger:
            logger.info("OAuth token refreshed via direct refresh grant")
        return tok
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

def _refresh_oauth_token(logger: logging.Logger = None) -> str:
    """Refresh the on-disk OAuth token; return the new access token, or "".

    The access token lives ~8h. Nothing on this box refreshes it in the gaps
    between sessions — the vendor CLI only refreshes as a side effect of running
    inference, and its read-only `auth status` command does not — so the token
    expires and every probe 401s, and an idle `remote-control` session that the
    user finally messages dead-ends at "Not logged in · Please run /login"
    (with /login unavailable in that environment). So we refresh it ourselves,
    directly, using the refresh token the CLI already stored.
    """
    tok = _oauth_refresh_grant(logger)
    if tok:
        return tok
    # Fallback: let the vendor CLI refresh as a side effect, if a variant that
    # actually does so is installed. `claude auth status` is read-only and does
    # NOT refresh (verified), so this rarely helps — it's a safety net for hosts
    # where the direct grant is blocked (e.g. sandboxed egress).
    for argv, returns_token in (
            (["ant", "auth", "print-credentials", "--access-token"], True),
            (["claude", "auth", "status"], False)):
        if not shutil.which(argv[0]):
            continue
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode != 0:
            continue
        if returns_token:
            tok = r.stdout.strip()
            # Never log the token itself — only whether we got one.
            if tok and "\n" not in tok:
                if logger:
                    logger.info(f"OAuth token refreshed via {argv[0]}")
                return tok
            continue
        tok = _read_token_file()
        if tok and _token_expires_at() - _now_sec() > TOKEN_SKEW_SEC:
            if logger:
                logger.info(f"OAuth token refreshed via {argv[0]}")
            return tok
    if logger:
        logger.warning(
            "OAuth token is expired and could not be refreshed automatically. "
            "Run `claude` (any session) to refresh it; /usage falls back to "
            "local estimation until then."
        )
    return ""

def _now_sec() -> float:
    import time as _time
    return _time.time()

def _read_token_file() -> str:
    try:
        creds = json.loads(CREDENTIALS_FILE.read_text())
        return creds.get("claudeAiOauth", creds).get("accessToken", "")
    except (OSError, ValueError):
        return ""

def _oauth_token(logger: logging.Logger = None) -> str:
    """Claude Code OAuth access token: env var, else ~/.claude/.credentials.json.

    Refreshes via the vendor CLI when the stored token is expired (or about to
    be), so the usage probe keeps working between Claude sessions.
    """
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if tok:
        return tok  # explicit override; we don't manage its lifetime
    exp = _token_expires_at()
    if exp and exp - _now_sec() <= REFRESH_AHEAD_SEC:
        fresh = _refresh_oauth_token(logger)
        if fresh:
            return fresh
    return _read_token_file()

def probe_rate_limits(logger: logging.Logger = None,
                      _retried_auth: bool = False) -> dict:
    """Live 5h/7d utilization from Anthropic's rate-limit response headers.

    Sends a 1-token Haiku request (negligible cost) and reads the
    anthropic-ratelimit-unified-* headers. Returns
    {"pct5", "reset5", "pctw", "resetw", "status", "now"} or {} on any
    failure (the reason is kept in _probe_cache["last_err"] for /health).
    Cached for PROBE_CACHE_SEC so /usage + watchdog don't double-probe.
    """
    import time as _time
    import urllib.request
    if _time.monotonic() - _probe_cache["at"] < PROBE_CACHE_SEC and _probe_cache["data"] is not None:
        return _probe_cache["data"]

    token = _oauth_token(logger)
    if not token:
        return {}

    def _send(model):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", method="POST",
            data=json.dumps({"model": model, "max_tokens": 1,
                             "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "content-type": "application/json",
            })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.headers, None
        except Exception as e:
            # A real 429 still carries the headers — precisely when we want them.
            return getattr(e, "headers", None), e

    known = _probe_cache.get("model")
    order = ([known] if known else []) + [m for m in PROBE_MODELS if m != known]
    h = e = None
    for model in order:
        h, e = _send(model)
        if h is not None and h.get("anthropic-ratelimit-unified-5h-utilization") is not None:
            _probe_cache["model"] = model
            break
        # Only a missing/refused model is worth trying the next one for; an
        # auth or network failure would fail the same way on every model.
        if getattr(e, "code", None) not in (404, 429):
            break
    if e is not None:
        if h is None or h.get("anthropic-ratelimit-unified-5h-utilization") is None:
            # A 401 here means the token went stale *after* the expiry check
            # above (e.g. revoked, or refreshed by another process). Refresh
            # once and retry before giving up.
            if getattr(e, "code", None) == 401 and not _retried_auth:
                if _refresh_oauth_token(logger):
                    return probe_rate_limits(logger, _retried_auth=True)
            # Don't repeat an identical failure every 30 min forever — the old
            # behaviour filled the log with hundreds of identical 401 lines.
            msg = str(e)
            if msg != _probe_cache.get("last_err"):
                _probe_cache["last_err"] = msg
                if logger:
                    logger.warning(f"Rate-limit probe failed: {e}")
            return {}
    _probe_cache["last_err"] = None

    def _pct(name):
        # Header value is a fraction of the cap (0.0–1.0), not a percentage.
        v = h.get(name)
        return float(v) * 100 if v is not None else None

    def _reset(name):
        v = h.get(name)
        try:
            return datetime.fromtimestamp(int(v), tz=timezone.utc) if v else None
        except ValueError:
            try:  # some deployments send ISO timestamps instead of epoch
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None

    data = {
        "now": datetime.now(timezone.utc),
        "pct5": _pct("anthropic-ratelimit-unified-5h-utilization"),
        "reset5": _reset("anthropic-ratelimit-unified-5h-reset"),
        "pctw": _pct("anthropic-ratelimit-unified-7d-utilization"),
        "resetw": _reset("anthropic-ratelimit-unified-7d-reset"),
        "status": h.get("anthropic-ratelimit-unified-5h-status", ""),
    }
    if data["pct5"] is None:
        return {}
    _probe_cache["at"] = _time.monotonic()
    _probe_cache["data"] = data
    return data

def launch_port(launch_sh: Path) -> int:
    """PORT= value from a project's launch.sh (0 = none/non-web)."""
    try:
        m = re.search(r"(?m)^PORT=(\d+)", launch_sh.read_text())
        return int(m.group(1)) if m else 0
    except OSError:
        return 0

def health_ok(port: int, timeout: float = 3.0) -> bool:
    """True if GET localhost:<port>/health answers 2xx."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://localhost:{port}/health", timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False

def tail_lines(path: Path, n: int, max_bytes: int = 262144) -> list:
    """Last n lines of a file, reading at most max_bytes from the end.

    launch.log and the bot log can grow large; reading them whole into RAM
    blocks the event loop and can OOM for chatty long-running jobs.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n else lines

# ── Trash retention ───────────────────────────────────────────────────────────

def purge_trash(cfg: dict, logger: logging.Logger = None) -> int:
    """Delete soft-deleted projects in .trash/ older than trash_retention_days.

    Trash dirs are named `<project>-YYYYMMDD-HHMMSS`; age is taken from that
    suffix when parseable, else from the directory mtime. Returns the count
    purged. retention_days <= 0 disables purging.
    """
    days = cfg.get("trash_retention_days", 20)
    if not days or days <= 0:
        return 0
    trash = Path(cfg["projects_root"]) / ".trash"
    if not trash.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=days)
    purged = 0
    for d in trash.iterdir():
        if not d.is_dir():
            continue
        m = re.search(r"(\d{8}-\d{6})$", d.name)
        if m:
            try:
                when = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
            except ValueError:
                when = datetime.fromtimestamp(d.stat().st_mtime)
        else:
            when = datetime.fromtimestamp(d.stat().st_mtime)
        if when < cutoff:
            try:
                shutil.rmtree(d)
                purged += 1
                if logger:
                    logger.info(f"Purged trash (>{days}d old): {d.name}")
            except OSError as e:
                if logger:
                    logger.warning(f"Could not purge {d}: {e}")
    return purged

# ── tmux helpers ──────────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

def bad_project_name(name: str) -> str:
    """Error text if `name` can't be a project, else "". Slash-command options
    are free text: `/delete project:../claude-pm` would have moved the bot's own
    source into .trash, and `/open ../x` would trust + spawn anywhere on disk.
    Dots, slashes and colons also break tmux targets."""
    if _NAME_RE.match(name or ""):
        return ""
    return (f"❌ `{name}` isn't a valid project name: letters, digits, `-` and "
            "`_` only.")

def session_name(cfg: dict, project: str) -> str:
    return f"{cfg['tmux_prefix']}-{project}"

def _tx(name: str) -> str:
    """Exact-match tmux session target. A bare `-t name` also matches any
    session whose name merely STARTS with `name` — so `/stop web` would have
    killed cp-webapp (verified 2026-10-15). `=` forces an exact match."""
    return f"={name}"

def _txp(name: str) -> str:
    """Exact-match target for pane commands (capture-pane, send-keys)."""
    return f"={name}:"

def session_exists(name: str) -> bool:
    r = subprocess.run(["tmux", "has-session", "-t", _tx(name)],
                       capture_output=True, timeout=10)
    return r.returncode == 0

def active_sessions(cfg: dict) -> list:
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return []
    pfx = cfg["tmux_prefix"] + "-"
    return [s for s in r.stdout.strip().splitlines() if s.startswith(pfx)]

# Sessions we killed on purpose, {session_name: monotonic timestamp}. The
# session watchdog consults this before firing 💀, so an intentional stop is no
# longer reported as "died unexpectedly" — it was, e.g. on 2026-08-23:
#   11:11:37 Stop session cp-batterycover: OK
#   11:12:01 Session watchdog: batterycover died
# Entries expire (see INTENTIONAL_KILL_TTL) so a *later* genuine death of the
# same project still alerts.
_intentional_kills: dict = {}
INTENTIONAL_KILL_TTL = 300

# On-disk state, so it survives a bot restart and is shared with BoxBot (which
# calls kill_session from its own process — an in-memory record there was
# invisible to this bot's watchdog, so its /debug stop alerted as a death).
STATE_DIR   = Path.home() / ".cache" / "claude-pm"
KILLS_FILE  = STATE_DIR / "intentional_kills.json"
SEEN_FILE   = STATE_DIR / "sessions_seen.json"
# Memory soft block: sessions file requests here (claude-memcheck --request),
# the owner's /mem grants land here and outlive a restart or a /revive.
MEMREQ_FILE   = STATE_DIR / "mem_requests.json"
MEMGRANT_FILE = STATE_DIR / "mem_grants.json"

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default

def _write_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(data))
    except OSError:
        pass  # best-effort: losing this only costs a spurious/missed alert

def _record_kill(name: str):
    now = _now_sec()
    kills = {k: t for k, t in _read_json(KILLS_FILE, {}).items()
             if now - t < INTENTIONAL_KILL_TTL}
    kills[name] = now
    _write_json(KILLS_FILE, kills)

def kill_session(name: str) -> bool:
    import time as _time
    # Record *before* killing: a watchdog tick can land between the two, and a
    # missed record would fire a false alert. A failed kill just leaves a stale
    # entry, which expires harmlessly.
    _intentional_kills[name] = _time.monotonic()
    _record_kill(name)
    r = subprocess.run(["tmux", "kill-session", "-t", _tx(name)],
                       capture_output=True, timeout=10)
    return r.returncode == 0

def was_killed_intentionally(name: str) -> bool:
    """True if `name` was killed via kill_session recently. Consumes the record."""
    import time as _time
    now = _time.monotonic()
    for k, t in list(_intentional_kills.items()):
        if now - t > INTENTIONAL_KILL_TTL:
            _intentional_kills.pop(k, None)
    mine = _intentional_kills.pop(name, None) is not None
    # The on-disk record covers kills by another process (BoxBot) or before a
    # bot restart. Consume it either way, so one kill suppresses one alert.
    kills = _read_json(KILLS_FILE, {})
    t = kills.pop(name, None)
    if t is not None:
        _write_json(KILLS_FILE, kills)
    return mine or (t is not None and _now_sec() - t < INTENTIONAL_KILL_TTL)

def _scope_unit(sname: str) -> str:
    """systemd --user scope unit base name for a session (sanitized)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sname)
    return f"claude-{safe}"

# Deliberately NOT named claude-*: the orphan reaper sweeps claude-*.scope,
# and reaping this one would kill the tmux server and every session with it.
TMUX_SCOPE = "cpm-tmux-server.scope"

def ensure_tmux_server_scope(logger: logging.Logger = None) -> bool:
    """Move the tmux server out of whichever service started it.

    tmux forks its server from the first `new-session`, so the server lands in
    the *caller's* cgroup — claude-pm.service (or boxbot.service). Only
    `KillMode=process` then stood between a bot restart and every session
    dying at once. systemd's StartTransientUnit accepts existing PIDs, so the
    live server is adopted into its own scope: no restart, no signal (verified
    on a live process 2026-10-15). Idempotent; call after any new-session.
    """
    try:
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{pid}"],
                           capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return False
    pid = (r.stdout.split() or [""])[0]
    if r.returncode != 0 or not pid.isdigit():
        return False  # no server running yet
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text().strip()
    except OSError:
        return False
    if cg.endswith("/" + TMUX_SCOPE):
        return True
    try:
        b = subprocess.run(
            ["busctl", "--user", "call", "org.freedesktop.systemd1",
             "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
             "StartTransientUnit", "ssa(sv)a(sa(sv))", TMUX_SCOPE, "fail",
             "2", "PIDs", "au", "1", pid,
             "Description", "s", "tmux server for claude-pm sessions", "0"],
            capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError) as e:
        b = None
        err = str(e)
    if b is not None and b.returncode == 0:
        if logger:
            logger.info(f"tmux server (pid {pid}) moved into {TMUX_SCOPE}")
        return True
    if logger:
        logger.warning(f"Could not move tmux server into {TMUX_SCOPE}: "
                       f"{b.stderr.strip() if b is not None else err}")
    return False

def _sctl(*args, timeout: float = 5.0):
    """Run `systemctl --user <args>`, or None if it failed, timed out, or is absent.

    Every caller here is best-effort cleanup, so a broken or hung systemctl must
    read as "couldn't tell" rather than raise.
    """
    try:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None

def _scope_gone(scope: str) -> bool:
    """True only if systemd positively reports `scope` as not-found.

    An unreadable LoadState is deliberately *not* "gone": treating unknown as
    free is what puts a launch on the "Unit was already loaded" path.
    """
    r = _sctl("show", scope, "-p", "LoadState", "--value")
    return bool(r) and r.returncode == 0 and r.stdout.strip() == "not-found"

def _free_scope_unit(unit: str, budget: float = 15.0) -> bool:
    """Release `<unit>.scope`'s name so systemd-run can reuse it.
    True once the name is free. Best-effort — never raises.

    Freeing it matters because systemd-run aborts with "Unit ... was already
    loaded" if the name is taken, and the session then dies instantly with an
    empty pane (a generic "died unexpectedly"). Two leftovers can hold it: a
    scope still *active* because a child (a Gradle/build daemon) outlived the
    tmux session — as happens when a crash or a power/wifi drop kills the
    session but not its orphaned children — or one left *failed* by a prior
    OOM/crash, which only reset-failed clears.

    The subtlety, and the 2026-10-15 /revive outage: a plain `systemctl stop`
    BLOCKS until every process in the scope is gone, so one child ignoring
    SIGTERM holds the client for TimeoutStopSec (~100s). The old code gave that
    call `timeout=10`, and subprocess.run raises TimeoutExpired on expiry rather
    than returning — the exception escaped _mem_scope_prefix and spawn_session,
    so /revive died on a traceback and webapp could not be brought back. Hence:
    request the stop asynchronously (--no-block), then escalate to SIGKILL, and
    poll for the name to actually clear.
    """
    import time as _time
    scope = f"{unit}.scope"

    def _wait_gone(secs: float) -> bool:
        end = _time.monotonic() + max(0.0, secs)
        while True:
            if _scope_gone(scope):
                return True
            if _time.monotonic() >= end:
                return False
            _time.sleep(0.4)

    if _scope_gone(scope):
        return True
    # --no-block: queue the stop and return at once, instead of waiting out
    # TimeoutStopSec on a child that won't take SIGTERM.
    _sctl("stop", "--no-block", scope)
    grace = min(3.0, budget)
    if not _wait_gone(grace):
        # Something is ignoring SIGTERM. Take the name back by force; these are
        # the leftovers of a session that is already dead.
        _sctl("kill", "--signal=KILL", scope)
        _wait_gone(budget - grace)
    _sctl("reset-failed", scope)
    # A just-SIGKILLed scope can take a moment to be collected; don't call that
    # a failure (it would needlessly drop the cap). Bounded by `budget` so the
    # whole helper stays within its stated time allowance.
    return _wait_gone(min(2.0, budget))

SESSIONS_SLICE = "claude-sessions.slice"
SLICE_FILE = Path.home() / ".config" / "systemd" / "user" / SESSIONS_SLICE
_slice_ok = {"cfg": None, "ok": False}

def ensure_sessions_slice(cfg: dict) -> bool:
    """Write/refresh the shared-ceiling slice unit. True if usable.

    Every session and job scope is placed in this slice, so its MemoryHigh /
    MemoryMax bound them *together*, on top of each scope's own cap. Cached
    per config so the unit file + daemon-reload happen once, not per spawn.
    """
    key = (cfg.get("sessions_total_mem_high"), cfg.get("sessions_total_mem_max"),
           cfg.get("sessions_total_swap_max"))
    if _slice_ok["ok"] and _slice_ok["cfg"] == key:
        return True
    high, mx, swap = key
    if not mx:
        return False
    body = ("# Managed by claude-pm (ensure_sessions_slice) — edit ~/.claude_pm.json\n"
            "[Unit]\nDescription=claude-pm sessions + jobs (shared memory ceiling)\n\n"
            "[Slice]\nMemoryAccounting=yes\n"
            + (f"MemoryHigh={high}\n" if high else "")
            + f"MemoryMax={mx}\n"
            + (f"MemorySwapMax={swap}\n" if swap else ""))
    try:
        if not SLICE_FILE.exists() or SLICE_FILE.read_text() != body:
            SLICE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(SLICE_FILE, body)
            r = _sctl("daemon-reload", timeout=30)
            if not r or r.returncode != 0:
                return False
    except OSError as e:
        logging.getLogger("claude_pm").warning(f"Sessions slice setup failed: {e}")
        return False
    _slice_ok["cfg"], _slice_ok["ok"] = key, True
    return True

# ── Memory grants: the cap is a soft block, not a wall ───────────────────────
# A session that genuinely needs more than its cap files a request from inside
# its own scope (`claude-memcheck --request 12G --reason "..."`). The bot posts
# it to Discord; `/mem action:grant` raises that session's caps live and
# records the grant here, so a /revive or a bot restart keeps it. Nothing is
# lifted without the owner saying so — and nothing above mem_grant_ceiling at all.

_SIZE_RE   = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT])?B?\s*$", re.I)
_SIZE_MULT = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}

def parse_size(text: str):
    """"12G" / "12 GB" / "12" → bytes, or None if it isn't a size.

    A bare number means GB: nobody asking for more memory means 12 bytes.
    """
    m = _SIZE_RE.match(text or "")
    if not m:
        return None
    return int(float(m.group(1)) * _SIZE_MULT[(m.group(2) or "G").upper()])

def fmt_size(n) -> str:
    """Bytes → the compact form systemd takes back ("10G", "1536M")."""
    if n is None:
        return ""
    if n % _SIZE_MULT["G"] == 0:
        return f"{n // _SIZE_MULT['G']}G"
    if n % _SIZE_MULT["M"] == 0:
        return f"{n // _SIZE_MULT['M']}M"
    return str(int(n))

def _sname_project(cfg: dict, sname: str) -> str:
    """Project behind a tmux session name (`cp-foo` / `job-foo` → `foo`)."""
    for pre in (cfg.get("tmux_prefix", "cp") + "-", JOB_PREFIX + "-"):
        if sname.startswith(pre):
            return sname[len(pre):]
    return sname

def read_mem_grants() -> dict:
    return _read_json(MEMGRANT_FILE, {})

def mem_caps_for(cfg: dict, project: str) -> tuple:
    """(high, max, swap) for a project: its grant where it has one, else the
    config defaults. Returned as systemd size strings."""
    g = read_mem_grants().get(project) or {}
    return (g.get("high") or cfg.get("session_mem_high"),
            g.get("max")  or cfg.get("session_mem_max"),
            g.get("swap") or cfg.get("session_mem_swap_max"))

def set_mem_grant(project: str, high: str, mx: str, swap: str = "",
                  reason: str = "") -> dict:
    """Record a grant (None/"" for `high` clears it). Returns the grant map."""
    grants = read_mem_grants()
    if not mx:
        grants.pop(project, None)
    else:
        grants[project] = {"high": high, "max": mx, "swap": swap,
                           "reason": reason, "at": _now_sec()}
    _write_json(MEMGRANT_FILE, grants)
    return grants

def apply_mem_caps_live(unit: str, high: str, mx: str, swap: str = "") -> tuple:
    """Push caps onto a *running* scope/slice. (ok, detail).

    `--runtime` because a transient scope has no unit file to write a
    persistent drop-in into; the grant file is what makes it outlive the
    session. Takes effect immediately, so a session that is already throttled
    unblocks without being restarted.
    """
    props = []
    if high:
        props.append(f"MemoryHigh={high}")
    if mx:
        props.append(f"MemoryMax={mx}")
    if swap:
        props.append(f"MemorySwapMax={swap}")
    if not props:
        return False, "nothing to set"
    r = _sctl("set-property", "--runtime", unit, *props, timeout=20)
    if r and r.returncode == 0:
        return True, " ".join(props)
    return False, ((r.stderr or r.stdout).strip()[:200] if r else "systemctl unavailable")

def read_mem_requests() -> dict:
    return _read_json(MEMREQ_FILE, {})

def resolve_mem_request(project: str, verdict: str) -> dict:
    """Mark a pending request handled (or drop it). Returns what it was."""
    reqs = read_mem_requests()
    req = reqs.pop(project, {})
    if verdict:
        req = {**req, "status": verdict, "closed_at": _now_sec()}
        reqs[project] = req
    _write_json(MEMREQ_FILE, reqs)
    return req

def _mem_scope_prefix(cfg: dict, sname: str) -> list:
    """argv that runs a session inside a memory-capped systemd --user scope,
    or [] if the cap is disabled, systemd-run is unavailable, or the scope name
    could not be freed.

    A runaway process (e.g. a mesh boolean that balloons to 12 GB) then hits
    MemoryMax and is killed alone, instead of thrashing swap for hours and
    finally OOM-ing the whole machine. Prepended to `bash -c <cmd>`.
    """
    high, mx, swap = mem_caps_for(cfg, _sname_project(cfg, sname))
    if not mx or not shutil.which("systemd-run"):
        return []
    unit = _scope_unit(sname)
    if not _free_scope_unit(unit):
        # Name still held by a wedged leftover. Launching capped would fail
        # outright ("Unit was already loaded"); launching uncapped loses only
        # the memory guardrail, and a session with no cap beats no session at
        # all. spawn_session's cap check logs the resulting watchdog blind spot.
        logging.getLogger("claude_pm").warning(
            f"Could not free {unit}.scope — launching {sname} UNCAPPED.")
        return []
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--unit={unit}"]
    if ensure_sessions_slice(cfg):
        argv.append(f"--slice={SESSIONS_SLICE}")
    if high:
        argv += ["-p", f"MemoryHigh={high}"]
    argv += ["-p", f"MemoryMax={mx}"]
    if swap:
        argv += ["-p", f"MemorySwapMax={swap}"]
    return argv

def _scope_procs(scope: str) -> list:
    """[(pid, comm, cmdline)] for every process in a systemd --user scope.
    [] if the scope is gone or unreadable."""
    r = _sctl("show", scope, "-p", "ControlGroup", "--value")
    cg = r.stdout.strip() if r and r.returncode == 0 else ""
    if not cg:
        return []
    try:
        pids = Path(f"/sys/fs/cgroup{cg}/cgroup.procs").read_text().split()
    except OSError:
        return []
    out = []
    for pid in pids:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes() \
                .replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue  # exited between the two reads
        out.append((int(pid), comm, cmd))
    return out

def _session_scopes() -> list:
    """Names of every loaded `claude-*.scope` unit (live or leftover)."""
    r = _sctl("list-units", "--all", "--plain", "--no-legend", "claude-*.scope")
    if not r or r.returncode != 0:
        return []
    return [l.split()[0] for l in r.stdout.splitlines() if l.split()]

def _all_tmux_sessions() -> set:
    """Every tmux session name on the default server — not just ours. BoxBot
    (gutsman.py) spawns `cp-boxbot` through this module, and the relogin flow
    uses an unprefixed name; anything with a live tmux session is not an orphan."""
    try:
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return set()
    return set(r.stdout.split()) if r.returncode == 0 else set()

def _is_orphan_scope(scope: str, live_units: set, procs: list) -> bool:
    """A session scope is an orphan only when BOTH:
      - no tmux session maps to it (its session is gone), and
      - no `claude remote-control` process is left inside it.
    Two independent signals, so a tmux hiccup or a naming mismatch alone can
    never get a working session killed."""
    if scope[:-len(".scope")] in live_units:
        return False
    return not any("remote-control" in cmd for _, _, cmd in procs)

def reap_session_scope(sname: str, logger: logging.Logger = None) -> str:
    """Kill whatever a dead/stopped session left running in its scope.
    Returns a one-line summary of what was reaped, or "" if nothing was there.
    Blocking (up to ~15s on a SIGTERM-ignoring child) — call off the event loop."""
    scope = f"{_scope_unit(sname)}.scope"
    if _scope_gone(scope):
        return ""
    return _reap(scope, _scope_procs(scope), logger)

def _reap(scope: str, procs: list, logger: logging.Logger = None) -> str:
    used, _ = _scope_mem_raw(scope)
    names = sorted({comm for _, comm, _ in procs})
    freed = _free_scope_unit(scope[:-len(".scope")])
    what = ", ".join(names[:6]) + (" …" if len(names) > 6 else "") or "no processes"
    mem = ("" if not used else f", {used / 1e9:.1f} GB" if used >= 1e9
           else f", {used / 1e6:.0f} MB")
    summary = f"{scope}: {len(procs)} process(es) ({what}{mem})"
    if logger:
        (logger.info if freed else logger.warning)(
            f"Scope reaper: {'reaped' if freed else 'FAILED to reap'} {summary}")
    return summary if freed else ""

def reap_orphan_scopes(logger: logging.Logger = None) -> list:
    """Sweep: reap every `claude-*.scope` whose session is gone.

    The global fix for the 2026-10-15 outage. A session dying (crash, OOM,
    /stop, wifi drop) kills its tmux pane, but processes that daemonize —
    Gradle daemons, the Android emulator + netsimd, adb — stay alive in the
    scope: still holding RAM against the next session, still squatting on the
    unit name, and (netsimd) ignoring SIGTERM. Nothing ever reaped them until a
    /revive of that same project tripped over them. Run on startup and every
    minute from the bot, so leftovers live for a minute, not forever.
    Returns the summaries of what was reaped.
    """
    live = {_scope_unit(s) for s in _all_tmux_sessions()}
    reaped = []
    for scope in _session_scopes():
        procs = _scope_procs(scope)
        if not _is_orphan_scope(scope, live, procs):
            continue
        summary = _reap(scope, procs, logger)
        if summary:
            reaped.append(summary)
    return reaped

def _scope_mem_raw(scope: str) -> tuple:
    """(MemoryCurrent, MemoryMax) bytes for a scope unit, None where unknown."""
    r = _sctl("show", scope, "-p", "MemoryCurrent", "-p", "MemoryMax", "--value")
    if not r or r.returncode != 0:
        return None, None
    vals = []
    for l in r.stdout.strip().splitlines()[:2]:
        try:
            vals.append(int(l.strip()))
        except ValueError:
            vals.append(None)
    vals += [None] * (2 - len(vals))
    return vals[0], vals[1]

# ── Global resource rules (~/.claude/CLAUDE.md) ─────────────────────────────
# Every Claude Code session on the box loads ~/.claude/CLAUDE.md, so the
# memory self-policing rules go there: they reach existing projects, new ones,
# and BoxBot's sessions alike, which a per-project RULES.md never would. The
# source of truth is templates/GLOBAL_RULES.md; the bot owns only the text
# between the markers and leaves anything else in the file alone.

GLOBAL_CLAUDE_MD  = Path.home() / ".claude" / "CLAUDE.md"
CLAUDE_BIN        = Path.home() / ".claude" / "bin"
MEMCHECK_DEST     = CLAUDE_BIN / "claude-memcheck"
SUDO_SHIM_DEST    = CLAUDE_BIN / "sudo"
LOCAL_BIN         = Path.home() / ".local" / "bin"
# Prepended to every session/job command: puts claude-memcheck and the sudo
# shim (→ /usr/local/sbin/cpm-sudo gate) first on PATH, and ~/.local/bin
# *last* so user-installed tools (rea, androguard, …) are reachable without
# shadowing the system `claude` the bot spawns and /selfupdate manages — both
# exist, and they are different installs.
SESSION_PATH_EXPORT = f'export PATH="{CLAUDE_BIN}:$PATH:{LOCAL_BIN}"; '
SUDO_GATE   = Path("/usr/local/sbin/cpm-sudo")
SUDO_ETC    = Path("/etc/cpm-sudo")
RULES_BEGIN = "<!-- claude-pm:resource-rules BEGIN (managed — edit claude-pm/templates/GLOBAL_RULES.md) -->"
RULES_END   = "<!-- claude-pm:resource-rules END -->"

def install_global_rules(logger: logging.Logger = None,
                         dest: Path = None, memcheck_dest: Path = None) -> bool:
    """Install/refresh the managed rules block and claude-memcheck.
    Idempotent; returns True if anything changed. Never raises — a failure
    here must not keep the bot from starting."""
    dest = dest or GLOBAL_CLAUDE_MD
    memcheck_dest = memcheck_dest or MEMCHECK_DEST
    changed = False
    try:
        block = (f"{RULES_BEGIN}\n"
                 f"{(TEMPLATE_DIR / 'GLOBAL_RULES.md').read_text().strip()}\n"
                 f"{RULES_END}")
        cur = dest.read_text() if dest.exists() else ""
        m = re.search(re.escape(RULES_BEGIN.split(" (")[0]) + r".*?" +
                      re.escape(RULES_END), cur, re.S)
        new = (cur[:m.start()] + block + cur[m.end():]) if m else \
              (cur.rstrip() + "\n\n" + block + "\n" if cur.strip() else block + "\n")
        if new != cur:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(dest, new)
            changed = True
        for name, dst in (("claude-memcheck", memcheck_dest),
                          ("sudo-shim", memcheck_dest.parent / "sudo"),
                          ("claude-share", memcheck_dest.parent / "claude-share")):
            src = TEMPLATE_DIR / name
            if not dst.exists() or dst.read_bytes() != src.read_bytes():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                dst.chmod(0o755)
                changed = True
        if changed and logger:
            logger.info(f"Global resource rules installed ({dest}, {memcheck_dest})")
    except OSError as e:
        if logger:
            logger.warning(f"Could not install global resource rules: {e}")
    return changed

# ── Sharing files to Discord (claude-share) ──────────────────────────────────
# A session's own "send file" tool can't deliver (no claude.ai project thread).
# Images are shown by Reading them, inline in the Claude app; that's the owner's
# preference. When he asks for files on Discord, `claude-share` drops a request
# here and share_queue_loop posts the files.
# Any same-user process can write this queue, so the bot doesn't trust it: a
# file is only posted if it's a regular file inside that project's folder
# (after resolving symlinks), within Discord's size limit, and not named like
# a secret.

SHARE_QUEUE_DIR = STATE_DIR / "share_queue"
SHARE_MAX_FILES = 10
SHARE_MAX_BYTES = 10 * 1024 * 1024
_SECRET_NAME_RE = re.compile(
    r"(^botFiles\.md$|^\.env|credentials|secret|token|^id_(rsa|ed25519|ecdsa)"
    r"|\.(pem|key|p12|pfx|keystore|jks)$)", re.I)

def check_share_file(root: Path, f: str):
    """(Path, "") if `f` may be posted for a project rooted at `root`, else
    (None, reason)."""
    try:
        p = Path(f).resolve()
        root = Path(root).resolve()
    except (OSError, RuntimeError) as e:
        return None, f"{f}: {e}"
    if not p.is_relative_to(root):
        return None, f"`{f}` is outside the project folder"
    if not p.is_file():
        return None, f"`{p.name}` is not a file (anymore)"
    if _SECRET_NAME_RE.search(p.name):
        return None, f"`{p.name}` looks like a secret, so it isn't posted"
    if p.stat().st_size > SHARE_MAX_BYTES:
        return None, f"`{p.name}` is over Discord's 10 MB limit"
    return p, ""

def take_share_requests(cfg: dict, queue: Path = None) -> list:
    """Drain the share queue: [(project, [Path], caption, [rejection])].
    Each request file is removed once read, posted or not, so a bad request
    can't repeat every pass."""
    queue = queue or SHARE_QUEUE_DIR
    out = []
    try:
        names = sorted(q for q in queue.glob("*.json"))
    except OSError:
        return out
    for q in names:
        try:
            req = json.loads(q.read_text())
        except (OSError, ValueError):
            req = None
        q.unlink(missing_ok=True)
        if not isinstance(req, dict):
            continue
        project = str(req.get("project") or "")
        files = req.get("files") or []
        caption = str(req.get("caption") or "")[:1500]
        if bad_project_name(project) or not isinstance(files, list):
            continue
        root = session_dir(cfg, project)
        if root is None:
            out.append((project, [], caption, ["no folder found for this project"]))
            continue
        ok, rejected = [], []
        for f in files[:SHARE_MAX_FILES]:
            p, why = check_share_file(root, str(f))
            (ok.append(p) if p else rejected.append(why))
        if len(files) > SHARE_MAX_FILES:
            rejected.append(f"only the first {SHARE_MAX_FILES} files are posted")
        out.append((project, ok, caption, rejected))
    return out

# ── Power / battery ───────────────────────────────────────────────────────────

POWER_SUPPLY_DIR = Path("/sys/class/power_supply")

def _read_sysfs(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""

def power_state(root: Path = POWER_SUPPLY_DIR) -> dict:
    """Read AC + battery state from sysfs.

    Returns {"on_ac": bool|None, "pct": int|None, "status": str}. on_ac is None
    when the machine exposes no adapter or battery at all (desktop/VM), which
    the power watchdog treats as "nothing to watch". Peripheral batteries
    (mice, headsets — scope "Device") are ignored.
    """
    adapters, batteries = [], []
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        supplies = []
    for d in supplies:
        kind = _read_sysfs(d / "type")
        if kind in ("Mains", "USB"):
            adapters.append(_read_sysfs(d / "online") == "1")
        elif kind == "Battery" and _read_sysfs(d / "scope") != "Device":
            batteries.append(d)

    pct, status = None, ""
    if batteries:
        cap = _read_sysfs(batteries[0] / "capacity")
        pct = min(100, int(cap)) if cap.isdigit() else None
        status = _read_sysfs(batteries[0] / "status")

    if adapters:
        on_ac = any(adapters)
    elif batteries:
        on_ac = status != "Discharging"
    else:
        on_ac = None
    return {"on_ac": on_ac, "pct": pct, "status": status}

def _fmt_dur(sec: float) -> str:
    return f"{int(sec)}s" if sec < 60 else _fmt_idle(sec)

class PowerMonitor:
    """Turns successive power_state() readings into Discord alert texts.

    - Unplug: one alert, but only after the reading holds for two polls, so a
      loose connector flickering for a second doesn't page the phone.
    - On battery: one more alert per level in `levels` the charge falls to.
    - Replug: one all-clear with how long it ran on battery.
    Starting up already on battery counts as an unplug.
    """

    def __init__(self, levels=(50, 25, 10)):
        self.levels = sorted({int(l) for l in levels}, reverse=True)
        self.on_ac = True
        self._pending = None
        self._warned: set = set()
        self._since = None

    def update(self, st: dict, now: float, mention: str = "") -> list:
        if not st or st.get("on_ac") is None:
            return []
        ac, pct = st["on_ac"], st.get("pct")
        pct_txt = f" ({pct}%)" if pct is not None else ""
        out = []

        if ac != self.on_ac:
            if self._pending != ac:
                self._pending = ac       # first sighting — confirm next poll
                return []
            self._pending = None
            self.on_ac = ac
            if not ac:
                self._since = now
                self._warned = {l for l in self.levels
                                if pct is not None and pct <= l}
                out.append(
                    f"🔌 {mention}The laptop **power cord is unplugged**, now "
                    f"running on battery{pct_txt}. Plug it back in before it "
                    f"dies and takes every session with it.")
            else:
                ran = f" after {_fmt_dur(now - self._since)} on battery" \
                    if self._since else ""
                self._since = None
                self._warned = set()
                out.append(f"✅ Power is back{ran}, charging{pct_txt}.")
            return out
        self._pending = None

        if not ac and pct is not None:
            crossed = [l for l in self.levels if pct <= l and l not in self._warned]
            if crossed:
                self._warned.update(crossed)
                icon = "🪫" if pct <= min(self.levels) else "🔋"
                out.append(
                    f"{icon} {mention}Still on battery, now at **{pct}%** "
                    f"({_fmt_dur(now - self._since)} unplugged). Plug in the "
                    f"power cord.")
        return out

def unit_mem_detail(unit: str) -> dict:
    """Memory picture of a scope/slice from its cgroup files, or {}.

    {"used": working set (current − inactive_file — page cache that a big
    build leaves behind is not pressure), "soft": MemoryHigh or MemoryMax,
    "max", "swap", "psi": PSI `some` avg60 %}. PSI is the signal that matters:
    it is the share of time the unit's tasks were stalled waiting on memory,
    i.e. it *is* thrashing, and it falls back to ~0 once that stops.
    """
    r = _sctl("show", unit, "-p", "ControlGroup", "--value")
    cg = r.stdout.strip() if r and r.returncode == 0 else ""
    if not cg:
        return {}
    base = Path(f"/sys/fs/cgroup{cg}")
    def num(name):
        try:
            v = (base / name).read_text().strip()
        except OSError:
            return None
        return int(v) if v.isdigit() else None
    cur = num("memory.current")
    if cur is None:
        return {}
    inactive = 0
    try:
        for line in (base / "memory.stat").read_text().splitlines():
            k, _, v = line.partition(" ")
            if k == "inactive_file":
                inactive = int(v)
    except (OSError, ValueError):
        pass
    psi = 0.0
    try:
        m = re.search(r"some avg10=[\d.]+ avg60=([\d.]+)",
                      (base / "memory.pressure").read_text())
        psi = float(m.group(1)) if m else 0.0
    except OSError:
        pass
    high, mx = num("memory.high"), num("memory.max")
    return {"used": max(0, cur - inactive), "soft": high or mx, "max": mx,
            "swap": num("memory.swap.current") or 0, "psi": psi}

def mem_alert_state(d: dict, soft_pct: float, psi_th: float) -> str:
    """"alert", "clear" (re-arm) or "" (hold) for a unit_mem_detail reading."""
    if not d or not d.get("soft"):
        return ""
    ratio = d["used"] / d["soft"] * 100
    if ratio >= soft_pct or d["psi"] >= psi_th:
        return "alert"
    if ratio < soft_pct - 10 and d["psi"] < psi_th / 5:
        return "clear"
    return ""

def session_mem(sname: str) -> tuple:
    """(current_bytes, max_bytes) for a session's systemd scope, or
    (None, None) if it has no scope / can't be read (e.g. cap disabled)."""
    unit = f"{_scope_unit(sname)}.scope"
    r = subprocess.run(
        ["systemctl", "--user", "show", unit,
         "-p", "MemoryCurrent", "-p", "MemoryMax", "--value"],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return None, None
    lines = [l.strip() for l in r.stdout.strip().splitlines()]
    def _to_int(s):
        try:
            return int(s)
        except (ValueError, TypeError):
            return None
    cur = _to_int(lines[0]) if len(lines) > 0 else None
    mx = _to_int(lines[1]) if len(lines) > 1 else None
    return cur, mx

def _fmt_idle(sec: float) -> str:
    """Humanize a duration for alerts: '45m', '3h 20m', '2d 4h'."""
    m = int(sec // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"

def project_path(cfg: dict, pname: str) -> Path:
    """On-disk directory for a project. The bot-dev session ("claude-pm") lives
    in the bot's own repo, outside projects_root — see CommandHandler._debug."""
    if pname == "claude-pm":
        return Path(__file__).resolve().parent
    return Path(cfg["projects_root"]) / pname

def session_idle_sec(cfg: dict, pname: str) -> float:
    """Seconds since a session last did *conversation* work, or None if unknown.

    Measured as the mtime of the newest transcript under the project's
    ~/.claude/projects/<encoded-path>/ directory — i.e. the last time Claude
    actually wrote a turn.

    Deliberately NOT tmux's own timers, both of which lie here:
      - `session_activity` never moves off `session_created` for these detached
        `claude remote-control` panes (measured: all three sessions reported
        activity == creation, one of them ~41h stale despite being messaged
        minutes earlier).
      - `window_activity` moves constantly because the TUI repaints its spinner,
        so an idle session looks busy (measured: iconsuite "active 13m ago" when
        its last real turn was 178m earlier).
    Transcript mtime tracks real work: a session grinding through a long task
    keeps writing turns and is never flagged, while one parked waiting on the owner
    goes quiet immediately — which is exactly the state worth warning about.
    """
    path = project_path(cfg, pname)
    proj_dir = Path.home() / ".claude" / "projects"
    # Claude Code encodes the absolute path by replacing separators with dashes.
    cands = [str(path).replace("/", "-")]
    if "." in str(path):  # some versions flatten dots the same way
        cands.append(str(path).replace("/", "-").replace(".", "-"))
    for enc in cands:
        d = proj_dir / enc
        if not d.is_dir():
            continue
        # Subagent transcripts count as activity too — a session may be doing
        # all its work inside subagents.
        mtimes = []
        for jf in [*d.glob("*.jsonl"), *d.glob("*/subagents/*.jsonl")]:
            try:
                mtimes.append(jf.stat().st_mtime)
            except OSError:
                continue
        if mtimes:
            return max(0.0, _now_sec() - max(mtimes))
    return None

# Below this, a session was used recently enough that "idle" is just noise in
# /list — the uptime already tells the story.
LIST_IDLE_MIN_SEC = 3600

JOB_PREFIX = "job"

def job_session_name(project: str) -> str:
    return f"{JOB_PREFIX}-{project}"

def active_jobs() -> list:
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return []
    pfx = JOB_PREFIX + "-"
    return [s for s in r.stdout.strip().splitlines() if s.startswith(pfx)]

def session_uptimes() -> dict:
    """{session_name: seconds since creation} for all tmux sessions."""
    r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name} #{session_created}"],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {}
    now = datetime.now().timestamp()
    out = {}
    for line in r.stdout.strip().splitlines():
        try:
            name, created = line.rsplit(" ", 1)
            out[name] = max(0, int(now - int(created)))
        except ValueError:
            pass
    return out

def _fmt_uptime(secs: int) -> str:
    if secs >= 86400:
        return f"{secs // 86400}d{(secs % 86400) // 3600}h"
    if secs >= 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 60}m"

def capture_pane(name: str) -> str:
    """Capture a tmux pane, joining wrapped lines so long URLs stay intact.

    The pane is only ~80 cols wide when detached, so the connection URL wraps
    across lines. `-J` rejoins wrapped lines; without it the URL is truncated.
    """
    r = subprocess.run(
        ["tmux", "capture-pane", "-J", "-t", _txp(name), "-p"],
        capture_output=True, text=True, timeout=10
    )
    return r.stdout

def _extract_env_url(text: str) -> str:
    """Pull the connection URL from tmux pane output.

    Fresh sessions show `https://claude.ai/code?environment=env_...`, while
    resumed (`--continue`) sessions show `https://claude.ai/code/session_...`.
    Match both forms.
    """
    m = re.search(r'https://claude\.ai/code[/?]\S+', text)
    return m.group(0) if m else ""

async def spawn_session(cfg: dict, project: str, resume: bool = False,
                        path: Path = None, fresh: bool = False,
                        session_id: str = "") -> tuple:
    """Spawn a `claude remote-control` session in a detached tmux session.

    `resume=True` (used by /revive and /refresh) forces a restart: it bypasses
    the "already running" guard and adjusts the status wording. `session_id`
    (from /revive) reattaches to that conversation explicitly with
    `--session-id`; whether it worked is revive_session's to check and say.
    `fresh=True` is /refresh, which removed the folder's bridge pointer first;
    it only changes the wording here.

    `path` overrides the default `<projects_root>/<project>` location — used by
    /dev to open a session on the bot's own source directory.
    """
    if path is None:
        path = Path(cfg["projects_root"]) / project
    if not path.exists():
        return False, f"Project folder not found: `{path}`"

    sname = session_name(cfg, project)
    if session_exists(sname) and not resume:
        # Try to grab URL from existing session
        url = _extract_env_url(capture_pane(sname))
        msg = f"Session `{sname}` is already running.\n"
        if url:
            msg += f"\n🔗 **Connect here:** {url}"
        else:
            msg += f"Open the **Claude app → Claude Code tab** and look for **{project}** 🟢"
        return False, msg

    # --spawn=same-dir: newer CLIs otherwise stop at an interactive
    # "spawn mode [1/2]" prompt and never print the connection URL.
    #
    # The model is NOT set here (2026-10-20). Until then the bot exported
    # ANTHROPIC_MODEL into every session, which remote-control passes on to the
    # sessions it spawns — and an env var outranks the app, so picking a model
    # in the Claude app didn't stick. The model is the app's choice now; the
    # bot has no opinion and no default.
    import shlex
    # ulimit -c 0: no core files from anything in the session. The Android
    # emulator alone left 1.3 GB of qemu cores in /var/lib/systemd/coredump;
    # systemd-coredump honours the limit and still logs the crash itself.
    #
    # --session-id (2026-10-26): a plain restart only reattaches to the
    # folder's last conversation if it was recorded within ~4 hours; after that
    # it silently starts a new one. --session-id reattaches regardless (tested
    # on a 13 h-old myproj session). It can't be combined with --spawn and runs
    # the single-session mode, which is all the bot ever uses.
    mode = (f"--session-id {shlex.quote(session_id)}" if session_id
            else "--spawn=same-dir")
    cmd = (f"ulimit -c 0; {SESSION_PATH_EXPORT}claude remote-control "
           f"--name {shlex.quote(project)} {mode}")
    # Run inside a memory-capped systemd scope so a runaway session is killed
    # alone rather than OOM-ing the whole laptop (see _mem_scope_prefix). []
    # when the cap is disabled, so the launch degrades to a plain bash -c.
    # to_thread: freeing a wedged leftover scope can take several seconds,
    # which on the event loop would stall Discord heartbeats.
    scope = await asyncio.to_thread(_mem_scope_prefix, cfg, sname)
    r = subprocess.run(
        ["tmux", "new-session", "-d", "-s", sname, "-c", str(path)]
        + scope + ["bash", "-c", cmd],
        capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        return False, f"Failed to start tmux session: {r.stderr.strip()}"
    await asyncio.to_thread(ensure_tmux_server_scope, logging.getLogger("claude_pm"))

    # Poll the tmux pane for the connection URL (takes a few seconds to connect).
    # Capture the pane *before* the alive-check each pass so that if the session
    # dies fast (e.g. a bad flag), we still hold its last output and can surface
    # the actual error line — previously the buffer was empty at death time, so
    # failures showed only a generic "died unexpectedly" with nothing in logs.
    # Older Claude versions showed an "Enable Remote Control? (y/n)" prompt; we
    # answer it only if it appears, rather than blindly injecting 'y'.
    url = ""
    last_pane = ""
    answered_prompt = False
    for _ in range(40):
        await asyncio.sleep(0.5)
        pane = capture_pane(sname)
        if pane.strip():
            last_pane = pane
        if not session_exists(sname):
            err = _last_error_line(last_pane)
            detail = f"\n```\n{err}\n```" if err else ""
            return False, f"Session died unexpectedly.{detail}\nCheck `logs` for details."
        if not answered_prompt and re.search(r"\(y/n\)|[Ee]nable [Rr]emote", last_pane):
            subprocess.run(["tmux", "send-keys", "-t", _txp(sname), "y", "Enter"],
                           capture_output=True, timeout=10)
            answered_prompt = True
            continue
        # Fallback for the spawn-mode prompt (in case a future CLI drops the
        # --spawn flag and asks interactively again): choose 1 = same-dir.
        if not answered_prompt and re.search(r"Choose \[1/2\]", last_pane):
            subprocess.run(["tmux", "send-keys", "-t", _txp(sname), "1", "Enter"],
                           capture_output=True, timeout=10)
            answered_prompt = True
            continue
        url = _extract_env_url(last_pane)
        if url:
            break

    # Verify the memory cap actually took effect. spawn and /revive both route
    # through _mem_scope_prefix, but a stale unit or tmux's own spawn scope can
    # silently leave the session UNCAPPED — as happened to a revived batterycover,
    # which ran in a `tmux-spawn-<uuid>` scope instead of claude-cp-batterycover,
    # invisible to the memory watchdog. If we intended a cap and none is present,
    # log it so that blind spot is visible instead of silent.
    if scope and session_exists(sname):
        _, cap_mem = session_mem(sname)
        if cap_mem is None:
            logging.getLogger("claude_pm").warning(
                f"Session {sname} launched WITHOUT its memory cap "
                f"(no {_scope_unit(sname)}.scope) — the mem watchdog can't see it."
            )

    verb = ("started fresh" if fresh else "restarted") if resume else "ready"
    msg = f"**{project}** {verb} — **to connect from your phone:**\n"
    if url:
        msg += f"\n🔗 **Link:** {url}\n\n"
    msg += (
        f"1. Open the **Claude app** or visit the link above\n"
        f"2. Tap **Claude Code** tab\n"
        f"3. Find **{project}** with a 🟢 and tap it\n"
    )
    if fresh:
        msg += (
            "\n🆕 *Brand-new conversation — it knows nothing of the old one. Point "
            "it at the project's LOG.md / handoff notes first. The old conversation "
            "is still in the app's session list if you need it.*\n"
        )
    msg += f"\nOr attach locally: `tmux attach -t {sname}`"
    return True, msg


def _last_error_line(pane: str) -> str:
    """Best-effort extraction of an error/warning line from pane text."""
    for line in reversed([l.strip() for l in pane.splitlines() if l.strip()]):
        if re.search(r"error|no conversation|cannot|failed|provide a prompt", line, re.I):
            return line[:200]
    return ""


def _classify_death(pane: str) -> tuple:
    """Best-effort cause of a `remote-control` session death, from its last
    captured pane. Returns (cause_markdown, usage_limited).

    Three buckets, because the right thing to tell the phone differs:
      - usage limit — the session was blocked; reviving now just re-hits it,
        so the alert should say "wait for the reset" instead of "restart it".
      - explicit error/crash — surface the offending line.
      - otherwise — on a laptop, most session deaths are the relay connection
        dropping after a wifi/suspend blip (as happened to batterycover +
        screwcap on 2026-08-25). That leaves no error in the pane, so an idle
        session with a clean pane is reported as a likely network drop rather
        than the old catch-all "died unexpectedly".
    """
    text = pane or ""
    m = re.search(
        r"(?:you'?ve\s+)?hit your (?:usage|session) limit[^\n]*"
        r"|usage limit[^\n]*reset[^\n]*",
        text, re.I)
    if m:
        return (f"It had hit its **usage limit** — `{m.group(0).strip()[:120]}`.",
                True)
    err = _last_error_line(text)
    if err:
        return (f"It hit an error:\n```\n{err}\n```", False)
    return ("Likely a **network/relay drop** — the connection to Claude was "
            "lost (no crash, no error in the session log).", False)


def session_dir(cfg: dict, name: str):
    """Working dir for a project or bot session, or None.

    Projects live under projects_root; claude-pm's own /debug session lives in
    the bot's repo (project_path); BoxBot's lives in ~/BoxBot, which only its
    live tmux session knows. /revive used to look under projects_root only, so
    reviving either bot session failed with "No project named".
    """
    p = project_path(cfg, name)
    if p.is_dir():
        return p
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t",
                            _txp(session_name(cfg, name)), "#{session_path}"],
                           capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return None
    d = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    return d if d and d.is_dir() else None

def _transcript_dir(path: Path):
    """~/.claude/projects/<encoded path>/ for a project dir, or None."""
    proj_dir = Path.home() / ".claude" / "projects"
    enc = str(path).replace("/", "-")
    for cand in (enc, enc.replace(".", "-")):
        if (proj_dir / cand).is_dir():
            return proj_dir / cand
    return None

def _msg_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""

def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 1] + "…"

def build_resume_recap(path: Path) -> str:
    """"Where we left off" for /revive, built from files: no model call.

    Newest LOG.md entry + the last request and reply from the most recent
    conversation. This replaced a one-shot `claude --continue` summary, which
    reloaded the whole conversation as input, waited up to 200s, ran outside
    any memory cap, leaked its process on timeout, and was pinned to a model
    ID that would eventually retire.
    """
    out = []
    try:
        log = (path / "LOG.md").read_text()[:200_000]
        parts = re.split(r"(?m)^## ", log)
        if len(parts) > 1:
            head, _, body = parts[1].partition("\n")
            out.append(f"**Last LOG.md entry:** {head.strip()}\n> {_clip(body, 450)}")
    except OSError:
        pass
    tdir = _transcript_dir(path)
    if tdir:
        files = sorted(tdir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        if files:
            last_user = last_reply = ""
            for line in tail_lines(files[-1], 0, max_bytes=4_000_000):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("isMeta"):
                    continue
                text = _msg_text(d.get("message") or {}).strip()
                # Skip tool results, system reminders and slash-command echoes.
                if not text or text.startswith("<"):
                    continue
                if d.get("type") == "user":
                    last_user = text
                elif d.get("type") == "assistant":
                    last_reply = text
            if last_user:
                out.append(f"**Last request:** {_clip(last_user, 300)}")
            if last_reply:
                out.append(f"**Last reply:** {_clip(last_reply, 600)}")
    return "\n".join(out)


async def revive_session(cfg: dict, project: str,
                         logger: logging.Logger = None) -> tuple:
    """Restart a stale session and attach a "where we left off" recap.

    Kills any existing tmux session (and its scope leftovers, via spawn), then
    spawns a new `claude remote-control` process in the same directory,
    reattached by ID to the conversation the folder's bridge pointer names.
    Until 2026-10-26 this relied on remote-control reattaching by itself, which
    it only does within ~4 hours, and the message always said "same
    conversation": batterycover, revived after 30 h, quietly got a new one. Now
    the pointer is read again afterwards and the message says which it was.
    /refresh (refresh_session) is the deliberate clean slate.
    """
    log = logger or logging.getLogger("claude_pm")
    path = session_dir(cfg, project)  # before the kill: bot sessions need it live
    if path is None:
        return False, f"No project named `{project}`."

    sname = session_name(cfg, project)
    if await asyncio.to_thread(session_exists, sname):
        await asyncio.to_thread(kill_session, sname)
        await asyncio.sleep(1)

    await asyncio.to_thread(trust_workspace, str(path))
    old = read_bridge_session(path)
    ok, msg = await spawn_session(cfg, project, resume=True, path=path,
                                  session_id=old)
    why_new = ""
    if not ok and old:
        # The conversation may have been archived or deleted in the app. A
        # running session beats a failed revive, so start a new one and say so.
        log.warning(f"Revive {project}: reattaching to {old} failed "
                    f"({msg.splitlines()[0] if msg else '?'}); starting a new one")
        why_new = "the old one couldn't be reopened (archived or deleted in the app?)"
        ok, msg = await spawn_session(cfg, project, resume=True, path=path)
    if ok:
        new = read_bridge_session(path)
        kept = bool(old) and new == old
        if not kept and not why_new:
            why_new = ("there was no earlier conversation on record" if not old
                       else "remote-control registered a different one")
        log.info(f"Revive {project}: {'same conversation' if kept else 'NEW conversation'}"
                 f" ({old or 'no pointer'} → {new or 'no pointer'})")
        msg += ("\n\n📜 *Same conversation as before (reattached by ID). If the "
                "conversation itself is stuck, `/refresh` starts a new one.*"
                if kept else
                f"\n\n🆕 *New conversation: {why_new}. It knows nothing of the old "
                "one, so point it at LOG.md first. The old one is still in the "
                "app's session list.*")
    if ok and cfg.get("revive_summary", True):
        recap = await asyncio.to_thread(build_resume_recap, path)
        if recap:
            msg += f"\n\n📋 **Where we left off:**\n{recap}"
    return ok, msg

# remote-control records the session it serves in this file, under the folder's
# ~/.claude/projects/<encoded path>/ dir, and on every restart reattaches to it.
# That's why /revive of myproj (2026-10-21) came back as the same conversation —
# still carrying the point where its decoder attempt had been stopped — when a
# clean slate was what was needed. /refresh moves the pointer aside.
BRIDGE_POINTER = "bridge-pointer.json"

def read_bridge_session(path: Path) -> str:
    """Session ID ("session_…") the folder's bridge pointer names, or ""."""
    tdir = _transcript_dir(path)
    try:
        sid = json.loads((tdir / BRIDGE_POINTER).read_text()).get("sessionId")
    except (TypeError, OSError, ValueError, AttributeError):
        return ""
    return sid if isinstance(sid, str) and re.fullmatch(r"[\w-]{1,128}", sid) else ""
REFRESH_WAIT_SEC = 300   # the server held a stopped folder ~90-180s in testing
REFRESH_RETRY_SEC = 15

async def refresh_session(cfg: dict, project: str,
                          logger: logging.Logger = None) -> tuple:
    """Stop a project's session and open a brand-new conversation in its folder.

    Unlike /revive, nothing of the old conversation is loaded: the bridge
    pointer is moved to bridge-pointer.prev.json so remote-control registers a
    new session instead of reattaching. Without a pointer the server refuses
    the folder ("already served by a terminal `claude remote-control`") until
    it notices the old process is gone — a few minutes, even after a clean
    SIGTERM — so the spawn is retried until REFRESH_WAIT_SEC. The refusal
    often exits before spawn_session can capture the pane, so any early death
    counts as that refusal. If it never comes up, the pointer is put back so
    the project is left as it was found.
    """
    log = logger or logging.getLogger("claude_pm")
    path = session_dir(cfg, project)  # before the kill: bot sessions need it live
    if path is None:
        return False, f"No project named `{project}`."

    sname = session_name(cfg, project)
    if await asyncio.to_thread(session_exists, sname):
        await asyncio.to_thread(kill_session, sname)
        await asyncio.to_thread(reap_session_scope, sname, log)

    tdir = _transcript_dir(path)
    pointer = tdir / BRIDGE_POINTER if tdir else None
    backup = tdir / "bridge-pointer.prev.json" if tdir else None
    moved = False
    if pointer and pointer.exists():
        pointer.replace(backup)
        moved = True

    await asyncio.to_thread(trust_workspace, str(path))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REFRESH_WAIT_SEC
    tries = 0
    while True:
        tries += 1
        ok, msg = await spawn_session(cfg, project, resume=True, fresh=True, path=path)
        held = "already served" in msg or "died unexpectedly" in msg
        if ok or not held or loop.time() >= deadline:
            break
        await asyncio.sleep(REFRESH_RETRY_SEC)
    log.info(f"Refresh {project}: {'OK' if ok else 'FAILED'} after {tries} attempt(s)"
             f"{' (pointer moved aside)' if moved else ''}")

    if not ok:
        if moved and not pointer.exists():
            backup.replace(pointer)
        if held:
            msg += ("\nThe server probably still thinks the old session is "
                    f"running. Wait a few minutes and `/refresh {project}` again.")
        return False, msg
    if cfg.get("revive_summary", True):
        recap = await asyncio.to_thread(build_resume_recap, path)
        if recap:
            msg += f"\n\n📋 **Where the old conversation left off:**\n{recap}"
    return ok, msg

# ── System health / remote recovery ────────────────────────────────────────────
# The 2026-09-26 OAuth-lockout incident (refresh grant 400s for ~9h, then the
# box appears to have gone to sleep/rebooted overnight with nobody watching)
# is what these exist to catch and fix from a phone: /health surfaces the
# symptoms, /relogin + /logincode complete a fresh OAuth login without ever
# touching the laptop, /reboot recovers a wedged box, /selfupdate keeps the
# CLI current so version drift doesn't reintroduce the same failure mode.

def _tcp_check(host: str, port: int = 443, timeout: float = 3.0) -> tuple:
    """(ok, detail) — DNS + TCP reachability, e.g. for discord.com/api.anthropic.com."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "reachable"
    except socket.gaierror as e:
        return False, f"DNS failed ({e})"
    except OSError as e:
        return False, f"unreachable ({e})"

def _claude_cli_versions() -> tuple:
    """(installed, latest) claude-code version strings; "" for either on failure."""
    installed = ""
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            installed = r.stdout.strip().split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    latest = ""
    try:
        r = subprocess.run(["npm", "view", "@anthropic-ai/claude-code", "version"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            latest = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return installed, latest

def _service_health(unit: str) -> dict:
    """{'ActiveState', 'SubState', 'NRestarts'} from `systemctl --user show`, {} on failure."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=ActiveState,SubState,NRestarts"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out

def _auth_status() -> dict:
    """Parsed `claude auth status` (read-only, does not refresh). {} on failure/timeout."""
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=15)
        return json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}

# The ONLY root commands the bot runs itself. install-sudo-gate.sh grants
# exactly these (plus the session gate, templates/cpm-sudo) in place of the old
# blanket NOPASSWD:ALL, which gave every Claude session root. Argument lists
# must match the sudoers lines byte for byte; absolute paths, so the match
# never depends on PATH.
SUDO_SHUTDOWN_REBOOT = ["sudo", "-n", "/usr/bin/shutdown", "-r", "+1"]
SUDO_SHUTDOWN_CANCEL = ["sudo", "-n", "/usr/bin/shutdown", "-c"]
SUDO_NPM_UPDATE      = ["sudo", "-n", "/usr/bin/npm", "install", "-g"]
SUDOERS_RULES_TEMPLATE = (
    "# Managed by claude-pm (install-sudo-gate.sh). Sessions reach root only via\n"
    "# the gate; the bot only for reboot and Claude Code updates.\n"
    "__OWNER__ ALL=(root) NOPASSWD: /usr/local/sbin/cpm-sudo\n"
    "__OWNER__ ALL=(root) NOPASSWD: /usr/bin/shutdown -r +1, /usr/bin/shutdown -c\n"
    "__OWNER__ ALL=(root) NOPASSWD: /usr/bin/npm ^install -g @anthropic-ai/claude-code@[0-9][0-9.]*$\n"
)
SUDOERS_RULES = SUDOERS_RULES_TEMPLATE.replace("__OWNER__", getpass.getuser())

# Deliberately unprefixed (not cp-/job-) so /list, active_sessions(), and the
# session watchdogs never see it — this is a login flow, not a project session.
RELOGIN_SESSION = "claude-pm-relogin"

async def start_relogin(logger: logging.Logger = None) -> tuple:
    """Spawn `claude auth login` in a detached tmux session and return its URL.

    This is the code-paste flow, not a localhost callback (redirect_uri points
    at platform.claude.com, which displays a code to copy) — so it completes
    fine from a phone with no network path back to this box. Finish with
    `/logincode <code>`.
    """
    if session_exists(RELOGIN_SESSION):
        kill_session(RELOGIN_SESSION)
        await asyncio.sleep(0.5)
    r = subprocess.run(
        ["tmux", "new-session", "-d", "-s", RELOGIN_SESSION, "-c", str(Path.home()),
         "claude", "auth", "login"],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return False, f"Failed to start login flow: {r.stderr.strip()}"
    ensure_tmux_server_scope(logger)
    last_pane = ""
    for _ in range(20):
        await asyncio.sleep(0.5)
        pane = capture_pane(RELOGIN_SESSION)
        if pane.strip():
            last_pane = pane
        if not session_exists(RELOGIN_SESSION):
            break
        m = re.search(r'https://claude\.com/cai/oauth/authorize\?\S*', last_pane)
        if m:
            url = m.group(0)
            # tmux capture-pane doesn't fully strip the OSC-8 hyperlink escape
            # the CLI prints the URL with, so the same URL shows up twice,
            # back-to-back with no separator — cut at the second copy.
            second = url.find("https://", 8)
            if second != -1:
                url = url[:second]
            if logger:
                logger.info("Relogin: login URL issued")
            return True, (
                f"🔑 Open this URL, log in, then copy the code it shows you:\n{url}\n\n"
                "Then run `/logincode <the code>` to finish."
            )
    if logger:
        logger.warning("Relogin: no URL captured before timeout")
    tail = "\n".join(last_pane.strip().splitlines()[-15:])
    return False, f"Didn't see a login URL yet.\n```\n{tail}\n```"

async def submit_relogin_code(code: str, logger: logging.Logger = None) -> tuple:
    """Send a pasted login code to the pending /relogin session and report the outcome."""
    if not session_exists(RELOGIN_SESSION):
        return False, "No login flow in progress. Run `/relogin` first."
    subprocess.run(["tmux", "send-keys", "-t", _txp(RELOGIN_SESSION), code.strip(), "Enter"],
                   capture_output=True, timeout=10)
    last_pane = ""
    for _ in range(20):
        await asyncio.sleep(0.5)
        pane = capture_pane(RELOGIN_SESSION)
        if pane.strip():
            last_pane = pane
        if "Login successful" in last_pane:
            kill_session(RELOGIN_SESSION)
            if logger:
                logger.info("Relogin: login successful")
            return True, "✅ Logged in successfully. `/health` to confirm."
        if "Invalid code" in last_pane:
            if logger:
                logger.warning("Relogin: invalid code submitted")
            return False, "❌ Invalid code — make sure it was copied in full, then retry `/logincode <code>`."
        if not session_exists(RELOGIN_SESSION):
            break
    tail = "\n".join(last_pane.strip().splitlines()[-15:])
    return False, f"⏳ No confirmation yet — current state:\n```\n{tail}\n```"

# ── Template renderer ─────────────────────────────────────────────────────────

def render_template(template_name: str, replacements: dict) -> str:
    path = TEMPLATE_DIR / template_name
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    text = path.read_text()
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text

def write_project_files(cfg: dict, data: dict):
    project_path = Path(cfg["projects_root"]) / data["name"]
    now = datetime.now().strftime("%Y-%m-%d")

    stack_key = data.get("stack_key", "0")
    label, lang, framework, test_fw, run_cmd = STACKS.get(stack_key, STACKS["0"])
    detail = data.get("stack_detail", "").strip()

    # The condensed menu rows (Web / Script / Native) carry no runtime of their
    # own — their follow-up answer picks it. Resolve before anything branches
    # on `lang`, so downstream code sees "Python"/"Rust" exactly as it did when
    # each framework had its own menu row.
    stack_group = lang   # the menu row, kept after `lang` resolves to a runtime
    if lang in _GROUP_DEFAULT:
        lang, framework, test_fw, run_cmd = resolve_runtime(stack_group, detail)
        label = f"{label} — {lang}" + (f" + {framework}" if framework else "")

    # Custom stack override
    if data.get("custom_stack"):
        lang = data["custom_stack"]
        framework = ""
        label = lang

    if lang == "CTF":
        ctf_cat = data.get("stack_detail", "").strip()
        stack_lines = f"- Category: {ctf_cat or 'general'}"
    elif lang == "RE":
        stack_lines = ("- Reverse engineering — REA harness (MCP server `rea`, "
                       "registered for this project)\n"
                       f"- Target: {detail or '(drop it in target/ and record its sha256)'}")
    elif lang == "CTFDEV":
        stack_lines = f"- CTF challenge authoring — {detail or 'category not set'}"
    elif lang:
        stack_lines = f"- Language: {lang}"
        if framework:
            stack_lines += f"\n- Framework: {framework}"
    else:
        stack_lines = "- Not specified — configure when you connect"

    if lang == "CTF":
        quickstart = (
            "- Drop challenge files into `assets/`\n"
            "- Add clues or context to `notes.md`\n"
            "- Claude will inventory `assets/` and read `notes.md` at session start"
        )
    elif lang == "RE":
        quickstart = (
            "- Put the artifact in `target/` and record its `sha256` in `LOG.md`\n"
            "- `export RE_TARGET=target/<file>` — the parsers in `tools/` read it\n"
            "- REA: use its MCP tools directly; `rea doctor --client claude_code "
            "--skill --json` only if they're missing\n"
            "- Findings go in `analysis/` as files, never only in chat"
        )
    elif lang == "CTFDEV":
        quickstart = (
            "- Write the intended solution path in `PLAN.md` before building anything\n"
            "- `build/` makes the player artifact; `solve/` holds the reference solver\n"
            "- Sweep every build: `strings`/`xxd`/`grep -r` must not reveal the flag"
        )
    elif run_cmd:
        quickstart = f"- Run: `{run_cmd}`\n"
        if test_fw:
            quickstart += f"- Test: `{test_fw}`"
        quickstart = quickstart.rstrip()
    else:
        quickstart = "- (Add your run and test commands here)"

    base = {
        "PROJECT_NAME":        data["name"],
        "PROJECT_PATH":        str(project_path),
        "PROJECT_DESCRIPTION": data.get("description", ""),
        "GENERATED_DATE":      now,
        "STACK_SECTION":       stack_lines,
        "QUICKSTART_SECTION":  quickstart.strip(),
        "TEST_FRAMEWORK":      test_fw or "your test framework",
    }

    # ── CLAUDE.md ──────────────────────────────────────────────────────────
    if lang == "CTF":
        goals_lines = (
            "- Find the flag\n"
            "- Document every step so the solve path can be reproduced"
        )
    else:
        goals_lines = "\n".join(
            f"- {g.strip()}" for g in data.get("goals", "").splitlines() if g.strip()
        ) or "- (Define goals when you connect)"

    if lang == "CTF":
        notes = (
            f"**Challenge category:** {detail or 'general'}\n\n"
            "When starting a session:\n"
            "1. `ls assets/` — inventory the challenge files\n"
            "2. Read `notes.md` — clues and prior observations\n"
            "3. Check `LOG.md` — what's been tried"
        )
    elif lang == "OpenSCAD":
        notes = (
            "**Print target (fixed hardware):**\n"
            f"- Printer: Prusa Core One, 0.4 mm nozzle\n"
            f"- Material: {detail or 'PLA (default — not specified in wizard)'}\n"
            "- Layer height: design must work at 0.15, 0.2, or 0.28 mm. Pick the\n"
            "  best one for this part (detail vs. speed vs. strength) and state\n"
            "  your choice + reasoning in LOG.md and to the owner.\n"
            "- Clearance: 0.1 mm default gap on all mating/connecting surfaces\n"
            "  (press-fits and living hinges may deviate — call it out when you do)\n\n"
            "**Copying an existing object?** Don't guess unknown dimensions or ask\n"
            "in prose. Render the model grey with each unknown as a coloured,\n"
            "numbered marker (`scripts/measure_lib.scad` + `render_measure.sh`),\n"
            "send the PNG + a matching numbered list, and let the owner answer by\n"
            "number. See SKILLS.md → \"Reverse-engineering an existing object\"."
        )
    elif lang == "RE":
        notes = (
            f"**Target:** {detail or '(not specified — record it in LOG.md)'}\n\n"
            "**Tooling:** the REA harness is registered for this project as the "
            "MCP server `rea` (CLI: `rea`). Ghidra 12.1.4 and a JDK 21+ are "
            "passed to it; an older system-wide `/opt/ghidra` may serve another project "
            "— don't repoint it.\n\n"
            "**SKILLS.md carries the full playbook** — evidence discipline, the "
            "triage→inventory→static→dynamic→verification workflow, the "
            "legacy-iOS/armv6 details (Thumb mode in `n_desc`, STABS, ObjC "
            "metadata, `objc_msgSend` typing), and the differential-testing "
            "loop that proves a decompilation instead of asserting it. "
            "**Read it before touching the binary.**\n\n"
            "`tools/` holds dependency-free Mach-O/STABS/ObjC parsers from the "
            "a prior legacy-iOS decompilation project; they read `$RE_TARGET`."
        )
    elif lang == "CTFDEV":
        notes = (
            f"**Challenge:** {detail or 'category not set'}\n\n"
            "You are **building** a challenge, not solving one. SKILLS.md has "
            "the authoring playbook: write the solution first, flag hygiene, "
            "hunting unintended solutions, difficulty calibration + hint "
            "ladder, and the hand-off package an event needs."
        )
    elif lang == "KiCad":
        notes = (
            f"**Board constraints:** {detail or '2-layer, JLCPCB (defaults — none specified)'}\n\n"
            "SKILLS.md has the full PCB workflow: fab design rules, ERC/DRC\n"
            "gate, headless kicad-cli commands. `./scripts/pcb_check.sh` is the\n"
            "test gate (ERC + DRC + render); the bot's `/render` posts the\n"
            "board image to Discord."
        )
    elif (lang == "GBDK" and "ctf" in detail.lower()
          and (_ASSETS / "gbdk" / "gbc-ctf").exists()):
        notes = (
            f"**ROM purpose:** GBC CTF challenge ROM ({detail})\n\n"
            "SKILLS.md carries the full GBC-CTF playbook — toolchain flags,\n"
            "phased build workflow, banking rules, and the layered code-hiding\n"
            "scheme. **Read it top to bottom before writing any code.**\n"
            "Reusable art, map tools, and the proven Makefile are in\n"
            "`GBC-Skills-Assets/`."
        )
    else:
        notes = f"**Hardware target:** {detail}" if detail else "(none)"
    claude_vars = {**base, "GOALS_SECTION": goals_lines, "NOTES_SECTION": notes}
    (project_path / "CLAUDE.md").write_text(render_template("CLAUDE.md.template", claude_vars))

    # ── RULES.md ───────────────────────────────────────────────────────────
    if lang == "CTF":
        custom_rules = "- Flag format: check the challenge description"
    else:
        custom_rules = "\n".join(
            f"- {r.strip()}" for r in data.get("rules", "").splitlines() if r.strip()
        ) or "- (No additional constraints specified)"

    if lang == "CTF":
        cat = data.get("stack_detail", "").strip().lower()
        # Word-boundary match for "re": as a substring it hits inside
        # "forensics" and would route that category to the RE branch.
        if re.search(r"\bre\b|\brev", cat):
            code_quality = (
                "- Run `file` and `strings` first on every binary before opening a decompiler\n"
                "- Note arch, compiler, and protections (`checksec`) immediately\n"
                "- Disassemble with `objdump -d` for a quick survey before Ghidra/radare2"
            )
            testing = "- Ghidra, radare2/Cutter, objdump, ltrace, strace, gdb"
        elif any(x in cat for x in ("pwn", "exploit", "binary")):
            code_quality = (
                "- Run `checksec` on every binary before anything else\n"
                "- Map input vectors, memory layout, and protections before attempting exploits\n"
                "- Write exploits in Python with pwntools; keep them in a single `exploit.py`"
            )
            testing = "- pwntools, gdb + peda/pwndbg, ROPgadget, checksec"
        elif any(x in cat for x in ("crypto", "cryptography")):
            code_quality = (
                "- Identify the cipher family before attempting any attack\n"
                "- Check for classic weaknesses: small exponent, reused nonce, ECB mode, bad RNG\n"
                "- Use SageMath for number-theory attacks; document the math in `notes.md`"
            )
            testing = "- Python cryptography / PyCryptodome, SageMath, CyberChef"
        elif "web" in cat:
            code_quality = (
                "- Map all endpoints and parameters before attempting any injection\n"
                "- Test OWASP top-10 systematically, not randomly\n"
                "- Document every request/response pair in `notes.md`"
            )
            testing = "- curl, ffuf, Burp Suite, sqlmap (with explicit permission)"
        elif any(x in cat for x in ("forensics", "forensic")):
            code_quality = (
                "- Run `file` and `exiftool` on every artifact before anything else\n"
                "- For images: check steganography (steghide, zsteg, stegsolve, binwalk)\n"
                "- For pcaps: review protocol summary before digging into packets"
            )
            testing = "- file, exiftool, binwalk, steghide, zsteg, wireshark/tshark, foremost"
        else:
            code_quality = (
                "- Enumerate all assets before going deep on any single one\n"
                "- Check encoding on suspicious strings (base64, hex, rot13, etc.)\n"
                "- When stuck: step back, re-read `notes.md`, try a different angle"
            )
            testing = "- Use whatever tools fit the challenge — document what you reach for and why"
    elif lang == "RE":
        code_quality = (
            "- State the bucket for every claim: observed / derived / inferred / unknown\n"
            "- Cite evidence as file + address/offset + the call that produced it —\n"
            "  \"the decompiler says\" is not a citation\n"
            "- Pin the build: record the target's sha256 in LOG.md; results from a\n"
            "  different version of the same artifact are not comparable\n"
            "- Every tool run writes its output to `analysis/` as a file\n"
            "- Prefer REA's tools over ad-hoc scripts; when REA can't do it, say so\n"
            "  and write the script into `tools/`"
        )
        testing      = (
            "- A function is recovered when it is **proven**, not when it reads well:\n"
            "  recompile it standalone and differential-test it against the original\n"
            "- Keep a per-unit ledger (verdict + evidence + coverage); % complete is\n"
            "  read from the ledger, never from memory\n"
            "- Check the checker: coverage audit, from-scratch rebuild, mutation test\n"
            "- Log every mechanical transform applied to recompile — each one is a claim"
        )
    elif lang == "CTFDEV":
        code_quality = (
            "- The reference solver in `solve/` must run end-to-end from a clean\n"
            "  environment against the deployed challenge\n"
            "- The flag is one constant, injected at build time, never pasted around\n"
            "- `build/` is deterministic: same source + flag → same artifact\n"
            "- Strip symbols, debug info, build paths and history from the artifact"
        )
        testing      = (
            "- Every build: `strings` / `xxd` / `grep -r` sweep — the flag must not\n"
            "  fall out, and look at the output rather than trusting the exit code\n"
            "- Hunt unintended solutions and record each one in `notes.md` as fixed\n"
            "  or accepted (with why)\n"
            "- Difficulty is rated by number of insights; ship a 3-step hint ladder\n"
            "- A challenge nobody else has playtested ships with that stated"
        )
    elif lang == "Python":
        code_quality = "- Follow PEP 8\n- Use type hints on all function signatures\n- Docstrings on public functions"
        testing      = f"- Use `{test_fw}` for all tests\n- Tests live in `tests/`\n- Cover all new code"
    elif lang == "JavaScript":
        code_quality = "- Follow ESLint rules\n- Use async/await, not raw callbacks\n- JSDoc on exported functions"
        testing      = f"- Use `{test_fw}` for all tests\n- Tests live in `__tests__/`\n- Cover happy path and errors"
    elif lang == "Rust":
        code_quality = "- No `unwrap()` in production paths\n- Run `cargo clippy` before committing"
        testing      = "- Use `#[test]` / `#[cfg(test)]` modules\n- Integration tests in `tests/`"
    elif lang == "Go":
        code_quality = "- Run `gofmt` before committing\n- Return errors, do not panic"
        testing      = "- Standard `testing` package\n- Table-driven tests preferred"
    elif lang == "OpenSCAD":
        code_quality = (
            "- All dimensions as named variables at the top; derive everything from them\n"
            "- One module per part; small named modules for features\n"
            "- Embed protruding features ~0.2 mm into parent body before union()\n"
            "- Target printer is always a Prusa Core One with 0.4 mm nozzle — walls in\n"
            "  multiples of 0.4 mm (0.8/1.2/1.6), min feature size ≥ 0.8 mm\n"
            "- Design for a layer height of 0.15, 0.2, or 0.28 mm; choose the best fit\n"
            "  for the part and report the choice (vertical features in multiples of it)\n"
            "- 0.1 mm clearance on mating/connecting surfaces by default; note any\n"
            "  intentional deviation (press-fit, thread, hinge) in a comment\n"
            "- Export binary STL only (`--export-format binstl`), never ASCII\n"
            "- Stamp `v#` on a hidden/inner face every revision\n"
            "- Set `$fa`/`$fs` inside each part module — file-scope values don't cross `use <>`\n"
            "- Reverse-engineering: tag every dimension [M] measured / [P] photo / [?] guess,\n"
            "  and request measurements as a colour-coded render (`measure.scad`), not prose"
        )
        testing      = (
            "- After every export: OpenSCAD CGAL summary + admesh + trimesh watertight check\n"
            "- Use `./scripts/export_and_validate.sh model.scad Module out.stl`\n"
            "- Zero disconnected facets, zero holes, body_count == 1 required to ship"
        )
    elif lang == "KiCad":
        code_quality = (
            "- Schematic before layout: ERC-clean + all footprints assigned before routing\n"
            "- Every IC power pin: 100 nF decoupling cap placed <2 mm away in layout\n"
            "- GND pour both layers with stitching vias; signals must not slice the pour\n"
            "- Design rules set to the fab's real limits in Board Setup (see SKILLS.md table)\n"
            "- Board name + version + date on silkscreen every revision\n"
            "- Exact MPN + package for every part logged in LOG.md before schematic work"
        )
        testing      = (
            "- `./scripts/pcb_check.sh` after every change — ERC + DRC clean required,\n"
            "  violations fixed or explicitly waived in LOG.md\n"
            "- Render and visually inspect (`/render`) after layout changes\n"
            "- Re-run DRC immediately before every gerber export"
        )
    elif lang == "GBDK":
        code_quality = (
            "- C only, GBDK-2020 conventions — no floats (use 8.8 fixed point), no malloc\n"
            "- Respect hardware limits: 40 sprites total, 10 per scanline, 8 KB VRAM,\n"
            "  8 KB WRAM (DMG) — track budgets in comments where they're spent\n"
            "- All graphics as assets converted via `png2asset`, never hand-coded tile bytes\n"
            "- Bank-aware from the start: mark code/data segments once ROM > 32 KB\n"
            "- `make` must always produce a working `build/*.gb` — never commit a broken build"
        )
        testing      = (
            "- `make` clean build = the test gate; zero warnings from lcc\n"
            "- Check ROM size and bank usage after every build (`make size` or the map file)\n"
            "- Verify visually in an emulator after gameplay-affecting changes\n"
            "- Test on DMG palette (4 shades) even if targeting CGB"
        )
        if "ctf" in data.get("stack_detail", "").lower():
            testing += (
                "\n- CTF ROM: before release, `strings`/`xxd` sweeps must find no code\n"
                "  fragments — run the full SKILLS.md §5–6 obfuscation checklist"
            )
    else:
        code_quality = "- (Define code quality standards when you connect)"
        testing      = "- (Define testing approach when you connect)"

    rules_vars = {**base,
                  "CODE_QUALITY_SECTION": code_quality,
                  "TESTING_SECTION":      testing,
                  "CUSTOM_RULES_SECTION": custom_rules}
    (project_path / "RULES.md").write_text(render_template("RULES.md.template", rules_vars))

    # ── PLAN.md ────────────────────────────────────────────────────────────
    overview = data.get("description", "")
    raw_plan = data.get("plan", "").strip()
    if lang == "CTF":
        phase1 = (
            "- `ls assets/` — inventory all challenge files\n"
            "- `file *`, `strings`, `xxd` first pass on everything\n"
            "- Document initial impressions in `notes.md`"
        )
        phase2 = (
            "- Deep dive on the most promising lead\n"
            "- Run category-specific tools (see RULES.md)\n"
            "- Document every finding and dead end in `notes.md`"
        )
        phase3 = (
            "- Craft the final exploit / decode / solution\n"
            "- Verify the flag format matches the expected pattern\n"
            "- Document the complete solve path in `LOG.md`"
        )
    elif lang == "RE":
        phase1 = (
            "- Triage: `file`, arch, packer/encryption, stripped or not, protections\n"
            "- Inventory everything into `analysis/` (segments, symbols, debug\n"
            "  records, resources, linked libraries)\n"
            "- Pin the build: sha256 in LOG.md"
        )
        phase2 = (
            "- Static model: decompile, type and name; seed what the tools can't infer\n"
            "- Dynamic ground truth: run the original, capture dispatch targets,\n"
            "  indirect calls and re-enterable states\n"
            "- Feed the runtime facts back into the static model"
        )
        phase3 = (
            "- Recompile and differential-test against the original from captured states\n"
            "- Ledger: verdict + evidence per unit; close the gaps\n"
            "- Check the checker (coverage, from-scratch rebuild, mutation test)"
        )
    elif lang == "CTFDEV":
        phase1 = (
            "- Write the intended solution path and name the single insight\n"
            "- Decide category, difficulty tier and flag format\n"
            "- Prototype the core mechanic"
        )
        phase2 = (
            "- Build the artifact deterministically; inject the flag at build time\n"
            "- Write the reference solver and run it end-to-end\n"
            "- Hunt unintended solutions; record each as fixed or accepted"
        )
        phase3 = (
            "- Strip and sweep the artifact (`strings`/`xxd`/`grep`)\n"
            "- Write the hint ladder and the hand-off package\n"
            "- Playtest with someone who doesn't know the answer"
        )
    elif raw_plan:
        lines = [l.strip() for l in raw_plan.splitlines() if l.strip()]
        third  = max(1, len(lines) // 3)
        phase1 = "\n".join(f"- {l}" for l in lines[:third])
        phase2 = "\n".join(f"- {l}" for l in lines[third:third*2])
        phase3 = "\n".join(f"- {l}" for l in lines[third*2:])
    else:
        phase1   = "- Set up project structure and tooling\n- Scaffold core modules\n- First passing tests"
        phase2   = "- Implement core features\n- Wire up integrations\n- Expand test coverage"
        phase3   = "- Polish and harden\n- Final test pass\n- Deploy"

    plan_vars = {**base,
                 "PLAN_OVERVIEW":  overview,
                 "PHASE_1":        phase1 or "- (Define foundation tasks)",
                 "PHASE_2":        phase2 or "- (Define core feature tasks)",
                 "PHASE_3":        phase3 or "- (Define polish and deploy tasks)",
                 "OPEN_QUESTIONS": "- (None noted — add as they come up)"}
    (project_path / "PLAN.md").write_text(render_template("PLAN.md.template", plan_vars))

    # ── SKILLS.md (+ per-stack scaffold bundle) ────────────────────────────
    scaffold = STACK_SCAFFOLDS.get(lang)
    # GBDK splits on purpose (wizard 3b): a CTF challenge ROM gets the
    # GBC-CTF playbook + reusable art/tools instead of the general scaffold.
    # That playbook is optional (not shipped here): drop your own into
    # scaffolds/gbdk/gbc-ctf; without it the general GBDK scaffold is used.
    if (lang == "GBDK" and "ctf" in detail.lower()
            and (_ASSETS / "gbdk" / "gbc-ctf").exists()):
        scaffold = _ASSETS / "gbdk" / "gbc-ctf"
    if scaffold is not None and not scaffold.exists():
        scaffold = None

    custom_skills = data.get("custom_skills", "").strip()
    if scaffold:
        skill_src = scaffold / "SKILL.md"
        scaffold_content = skill_src.read_text() if skill_src.exists() else ""
        if custom_skills:
            custom_skills = scaffold_content + "\n\n---\n\n" + custom_skills
        else:
            custom_skills = scaffold_content
    if not custom_skills:
        custom_skills = "*(Add project-specific prompt patterns here as you discover them)*"

    skills_vars = {**base, "CUSTOM_SKILLS_SECTION": custom_skills}
    (project_path / "SKILLS.md").write_text(render_template("SKILLS.md.template", skills_vars))

    # ── LOG.md (change journal — the hard rule in RULES.md points here) ───
    extra = (", tools/setup.sh"
             + (", LAUNCH.md, launch.sh"
                if (data.get("launch_type") or launch_type_for(stack_group)) != "none"
                else ""))
    log_vars = {**base, "LOG_EXTRA_FILES": extra}
    (project_path / "LOG.md").write_text(render_template("LOG.md.template", log_vars))

    # ── CTF: assets/ directory and notes.md ──────────────────────────────
    if lang == "CTF":
        (project_path / "assets").mkdir(exist_ok=True)
        ctf_cat = data.get("stack_detail", "").strip()
        notes_md = (
            f"# Notes: {data['name']}\n\n"
            f"{data.get('description', '')}\n\n"
            "---\n\n"
            "## Assets\n"
            "*(drop challenge files in `assets/` — run `file *`, `strings`, `xxd` to inventory)*\n\n"
            "## Leads\n"
            "*(promising threads to pull)*\n\n"
            "## Dead Ends\n"
            "*(what didn't work — log so you don't repeat it)*\n\n"
            "## Flag\n"
            "*(paste the flag here when found)*\n"
        )
        (project_path / "notes.md").write_text(notes_md)

    # ── tools/ — every project gets script scaffolding + the sudo path ────
    # Until 2026-10-20 the wizard asked each project what scripts it needed and
    # whether it wanted sudo. Both answers are the same for every project now
    # that the sudo gate exists: scripts live in tools/, and anything needing
    # root goes in tools/setup.sh and is approved once, per project, by hash.
    tools_dir = project_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    setup_sh = tools_dir / "setup.sh"
    if not setup_sh.exists():
        setup_sh.write_text(
            "#!/usr/bin/env bash\n"
            "# Root setup for " + data["name"] + ".\n"
            "#\n"
            "# Sessions have NO general sudo. This script is how this project gets\n"
            "# root, and only after the owner approves it by hash from Discord:\n"
            "#\n"
            "#   /sudo action:approve project:" + data["name"] + " script:tools/setup.sh\n"
            "#\n"
            "# Then run it as:  sudo tools/setup.sh\n"
            "# Editing this file cancels the approval — ask again after changes.\n"
            "#\n"
            "# Rules: keep it short, idempotent (safe to re-run), commented, and\n"
            "# limited to the work that actually needs root. Everything else belongs\n"
            "# in a normal script next to it in tools/.\n"
            "set -euo pipefail\n\n"
            "if [ \"$(id -u)\" -ne 0 ]; then\n"
            "  echo \"Run as: sudo tools/setup.sh (needs /sudo approval first)\" >&2\n"
            "  exit 1\n"
            "fi\n\n"
            "# --- put root-only setup below (package installs, udev rules, mounts) ---\n"
            "echo \"nothing to do yet — add this project's root setup here\"\n")
        setup_sh.chmod(0o755)

    # ── Stack scaffold payload: everything except SKILL.md → project root ──
    if scaffold:
        for item in scaffold.iterdir():
            if item.name == "SKILL.md":
                continue
            dst = project_path / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(item, dst)

    # ── LAUNCH.md + launch.sh ─────────────────────────────────────────────
    launch_type = data.get("launch_type") or launch_type_for(stack_group)
    if launch_type != "none":
        lt_labels = {"web": "Web server", "cli": "CLI tool / script", "service": "Background service"}
        # Web ports are auto-assigned by the wizard (next_web_port counter) so
        # concurrent jobs never collide; non-web projects use 0.
        port = str(data.get("port", 8000)) if launch_type == "web" else "0"

        if launch_type == "web":
            lt_section = (
                f"- **Type:** {lt_labels[launch_type]}\n"
                f"- **Port:** {port} (auto-assigned — unique per project)\n"
                "- **Health endpoint:** `GET /health` must return HTTP 200"
            )
            # Scaffold a minimal app with a working /health so `/run` succeeds
            # on day one and Claude builds on a running server. Framework-
            # idiomatic where deps are expected (FastAPI/Flask); plain node:http
            # for JS (zero deps).
            app_file, app_code, exec_line = "", "", ""
            if framework == "FastAPI":
                app_file = "main.py"
                app_code = (
                    "from fastapi import FastAPI\n\n"
                    "app = FastAPI()\n\n\n"
                    "@app.get(\"/health\")\n"
                    "def health():\n"
                    "    return {\"status\": \"ok\"}\n"
                )
                exec_line = ("exec $SUDO python3 -m uvicorn main:app --host 0.0.0.0 "
                             "--port \"$PORT\" >> \"$LOG_FILE\" 2>&1")
            elif framework == "Flask":
                app_file = "main.py"
                app_code = (
                    "import os\n\n"
                    "from flask import Flask\n\n"
                    "app = Flask(__name__)\n\n\n"
                    "@app.get(\"/health\")\n"
                    "def health():\n"
                    "    return {\"status\": \"ok\"}\n\n\n"
                    "if __name__ == \"__main__\":\n"
                    "    app.run(host=\"0.0.0.0\", port=int(os.environ.get(\"PORT\", "
                    + port + ")))\n"
                )
                exec_line = ("exec $SUDO env PORT=\"$PORT\" python3 main.py "
                             ">> \"$LOG_FILE\" 2>&1")
            elif lang == "JavaScript":
                app_file = "index.js"
                app_code = (
                    "// Minimal server with /health — replace with the real app "
                    "(keep the /health route).\n"
                    "const http = require('http');\n"
                    "const port = process.env.PORT || " + port + ";\n\n"
                    "http.createServer((req, res) => {\n"
                    "  if (req.url === '/health') {\n"
                    "    res.writeHead(200, {'Content-Type': 'application/json'});\n"
                    "    return res.end(JSON.stringify({status: 'ok'}));\n"
                    "  }\n"
                    "  res.writeHead(404);\n"
                    "  res.end();\n"
                    "}).listen(port, () => console.log(`listening on ${port}`));\n"
                )
                exec_line = ("exec $SUDO env PORT=\"$PORT\" node index.js "
                             ">> \"$LOG_FILE\" 2>&1")

            if app_file and not (project_path / app_file).exists():
                (project_path / app_file).write_text(app_code)
            if exec_line:
                cmd_placeholder = exec_line
            else:
                cmd_placeholder = (
                    "# TODO: Replace with your web server command, e.g.:\n"
                    f"# exec $SUDO python -m uvicorn main:app --host 0.0.0.0 --port \"$PORT\" >> \"$LOG_FILE\" 2>&1\n"
                    "echo \"[$(date -Iseconds)] ERROR: launch command not configured yet\" | tee -a \"$LOG_FILE\"\n"
                    "exit 1"
                )
        elif launch_type == "cli":
            lt_section = f"- **Type:** {lt_labels[launch_type]}\n- Runs to completion then exits"
            cmd_placeholder = (
                "# TODO: Replace with your run command, e.g.:\n"
                "# exec $SUDO python main.py \"$@\" >> \"$LOG_FILE\" 2>&1\n"
                "echo \"[$(date -Iseconds)] ERROR: launch command not configured yet\" | tee -a \"$LOG_FILE\"\n"
                "exit 1"
            )
        else:  # service
            lt_section = f"- **Type:** {lt_labels[launch_type]}\n- Long-running process, no network port"
            cmd_placeholder = (
                "# TODO: Replace with your service command, e.g.:\n"
                "# exec $SUDO python worker.py >> \"$LOG_FILE\" 2>&1\n"
                "echo \"[$(date -Iseconds)] ERROR: launch command not configured yet\" | tee -a \"$LOG_FILE\"\n"
                "exit 1"
            )

        # Sudo is no longer a wizard question: launch.sh starts unprivileged
        # (SUDO expands to nothing) and this section says exactly what to do if
        # the project turns out to need root.
        sudo_section = (
            "- **Default:** none. `SUDO=` is empty, so launch.sh runs as the normal user.\n"
            "- **If this project needs root:** prefer putting that work in\n"
            f"  `tools/setup.sh` and approving it once (`/sudo action:approve project:"
            f"{data['name']} script:tools/setup.sh`), so the long-running process itself\n"
            "  stays unprivileged.\n"
            "- **If the launch itself must be root** (binds a port <1024, needs device\n"
            "  access): set `SUDO=sudo` in launch.sh, use `$SUDO` before the exec line,\n"
            f"  and ask the owner for `/sudo action:approve project:{data['name']} "
            "script:launch.sh`.\n"
            "- Sessions have **no** general sudo, and editing an approved script cancels\n"
            "  its approval — ask again after changes."
        )
        sudo_var = ""

        launch_vars = {**base,
                       "LAUNCH_TYPE_SECTION":        lt_section,
                       "LAUNCH_SUDO_SECTION":       sudo_section,
                       "LAUNCH_PORT":                port,
                       "LAUNCH_SUDO":                sudo_var,
                       "LAUNCH_COMMAND_PLACEHOLDER": cmd_placeholder}
        (project_path / "LAUNCH.md").write_text(render_template("LAUNCH.md.template", launch_vars))
        (project_path / "launch.sh").write_text(render_template("launch.sh.template", launch_vars))
        (project_path / "launch.sh").chmod(0o755)

        # Add launch.log to .gitignore
        gi = project_path / ".gitignore"
        existing = gi.read_text() if gi.exists() else ""
        if "launch.log" not in existing:
            with open(gi, "a") as f:
                f.write("\nlaunch.log\n")

# ── Wizard ─────────────────────────────────────────────────────────────────────
#
# Condensed 2026-10-20 from nine steps to at most six. Two of the old steps
# asked things the project type already answers — what kind of launch script to
# write, and whether it needs sudo — so both are now inferred: every project
# gets a `tools/setup.sh` stub for the work that needs root (approved per
# project with /sudo) and a launch.sh whose type comes from the stack. The
# plan and prompt-pattern steps went too: both were nearly always skipped, and
# the first session writes better ones than a one-line answer on a phone.

WIZARD_IDLE        = "idle"
WIZARD_NAME        = "name"
WIZARD_DESCRIPTION = "description"
WIZARD_STACK       = "stack"
WIZARD_STACK_DETAIL = "stack_detail"
WIZARD_GOALS       = "goals"
WIZARD_RULES       = "rules"
WIZARD_CONFIRM     = "confirm"

STACK_MENU = (
    "What kind of project is this?\n```\n"
    + "\n".join(f"  {k}: {v[0]}" for k, v in STACKS.items())
    + "\n```\nReply with the number, or just name the stack (`fastapi`, `rust`, `ghidra`)."
)

# Short aliases for things whose menu label doesn't reduce to what a user would
# naturally type — normalized (lowercase, non-alphanumerics stripped) before
# lookup. The single letters are the pre-2026-10-20 menu keys, kept so the
# muscle memory from the old menu still lands on the right row.
_STACK_ALIASES = {
    "a": "8", "scad": "8", "3dprint": "8", "3d": "8",
    "b": "7", "gameboy": "7", "gb": "7", "gbdk": "7", "game": "7", "gamedev": "7",
    "c": "5", "ctfsolve": "5", "ctfplaythrough": "5", "playthrough": "5",
    "d": "9", "pcb": "9",
    "e": "4", "rea": "4", "reverseengineering": "4", "reversing": "4",
    "ghidra": "4", "decompile": "4", "decompilation": "4", "binary": "4",
    "ctfbuild": "6", "ctfdesign": "6", "challenge": "6",
    "webapp": "1", "api": "1", "website": "1",
    "cli": "2", "tool": "2", "systems": "3",
}

def _resolve_stack_key(text: str) -> str:
    """Resolve free-text input to a STACKS key, or "" when nothing matches.

    The menu invites typing a name, so this matches, in order: exact key
    (case-insensitive) → alias table → normalized match against the row's label
    or lang. Without it, typing the label right next to the number fell through
    to the generic custom path and skipped every type-specific question and
    scaffold.
    """
    text_upper = text.upper()
    if text_upper in STACKS:
        return text_upper
    norm = re.sub(r"[^a-z0-9]", "", text.lower())
    if not norm:
        return ""
    if norm in _STACK_ALIASES:
        return _STACK_ALIASES[norm]
    for key, (label, lang, *_rest) in STACKS.items():
        if norm == re.sub(r"[^a-z0-9]", "", label.lower()):
            return key
        if lang and norm == re.sub(r"[^a-z0-9]", "", lang.lower()):
            return key
    return ""

def resolve_stack_input(text: str) -> tuple:
    """(key, prefilled_detail) for whatever the user typed at the type step.

    Naming a runtime the condensed rows cover ("fastapi", "next.js", "rust")
    picks that row *and* answers its follow-up question, so the common case is
    one message shorter. Anything else is a custom stack: ("", "").
    """
    key = _resolve_stack_key(text)
    if key:
        return key, ""
    lang, fw, _test, _run = resolve_runtime("", text)
    if lang:
        group = "Web" if fw in ("FastAPI", "Flask", "Django", "Next.js",
                                "Express", "Vite") else "Script"
        if lang in ("Rust", "Go", "C/C++"):
            group = "Native"
        for k, (_label, glang, *_r) in STACKS.items():
            if glang == group:
                return k, text
    return "", ""

# One follow-up question per type, asked only where the answer changes what
# gets written. Keyed by lang.
STACK_DETAIL_QUESTIONS = {
    "Web": ("Which language and framework?\n"
            "*(e.g. `python fastapi`, `next.js`, `node express`, `django`)*\n"
            "Type `skip` for Python + FastAPI."),
    "Script": ("Which language?\n"
               "*(e.g. `python`, `node`, `bash`, `rust`, `go`)*\n"
               "Type `skip` for Python."),
    "Native": ("Which language, and what is it?\n"
               "*(e.g. `rust, indexing daemon`, `c++, audio plugin`)*\n"
               "Type `skip` for Rust."),
    "RE": ("What's the target, and what do you want out of it?\n"
           "```\n"
           "  e.g. iOS .ipa (armv7 Mach-O) — decompile to C and port\n"
           "       Android .apk — understand the licence check\n"
           "       x86-64 ELF — find the protocol it speaks\n"
           "       firmware .bin — recover the update format\n"
           "```\n"
           "Name the platform and the question you want answered."),
    "CTF": ("What's the challenge category?\n"
            "```\n"
            "  re         reverse engineering\n"
            "  pwn        binary exploitation\n"
            "  crypto     cryptography\n"
            "  web        web exploitation\n"
            "  forensics  digital forensics\n"
            "  misc       miscellaneous / unknown\n"
            "```\n"
            "Reply with the category (or describe it freely)."),
    "CTFDEV": ("What are you building, and for which category?\n"
               "```\n"
               "  gb         Game Boy / GBC challenge ROM (built as a Game Boy project)\n"
               "  re, pwn, crypto, web, forensics, misc\n"
               "```\n"
               "Add the difficulty and the intended solve path if you know them."),
    "GBDK": ("What kind of Game Boy ROM is this?\n"
             "```\n"
             "  game   standard game — DMG-first, general dev scaffold\n"
             "  ctf    GBC CTF challenge ROM — hidden codes,\n"
             "         obfuscation playbook + reusable art/tools\n"
             "```\n"
             "Add cartridge details after if you know them "
             "*(e.g. `game, MBC5, Color-first`)*."),
    "KiCad": ("What are the board constraints?\n"
              "*(layers, fab, power/interfaces — e.g. "
              "`2-layer, JLCPCB, USB-C powered` — drives design "
              "rules and stackup)*"),
    "OpenSCAD": ("Which material will this print in?\n"
                 "*(PLA / PETG / ASA / TPU — drives tolerances and wall thickness)*\n"
                 "Type `skip` for PLA."),
}

# Types where goals/rules add nothing the type itself doesn't already say:
# a CTF solve's goal is the flag, and a 3D part's rules are the printer's.
NO_GOALS_LANGS = {"CTF", "OpenSCAD"}

GOALS_PROMPT_BODY = (
    "What are the main goals?\n"
    "*(One per line — these become your CLAUDE.md goals)*"
)
RULES_PROMPT_BODY = (
    "Any hard rules or constraints?\n"
    "*(E.g. `no external API calls`, `mobile-first UI`, `no ORM — raw SQL only`)*\n"
    "Type `skip` if none."
)
STEP_PROMPTS = {WIZARD_GOALS: GOALS_PROMPT_BODY, WIZARD_RULES: RULES_PROMPT_BODY}


class ProjectWizard:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg   = cfg
        self.log   = logger
        self.state = WIZARD_IDLE
        self.data  = {}

    def reset(self):
        self.state = WIZARD_IDLE
        self.data  = {}

    def is_active(self) -> bool:
        return self.state != WIZARD_IDLE

    def cancel(self) -> str:
        self.log.info("Wizard cancelled")
        self.reset()
        return "❌ Wizard cancelled. Type `/new` to start again."

    # ── Flow ──────────────────────────────────────────────────────────────
    # The question list depends on the type, so it's computed rather than
    # numbered by hand: that's what let the old flow drift into claiming
    # "Step 4 of 5" in one branch and "Step 6 of 9" in another.

    def _lang(self) -> str:
        return STACKS.get(self.data.get("stack_key", "0"), STACKS["0"])[1]

    def _flow(self) -> list:
        """Ordered question states for the type chosen so far (no confirm)."""
        lang = self._lang()
        steps = [WIZARD_NAME, WIZARD_DESCRIPTION, WIZARD_STACK]
        # `detail_prefilled`: they named a runtime at the type step, which
        # already answers the follow-up — so it is not a question in this flow.
        # (Answering it normally must NOT drop it, or _advance would jump
        # straight to the summary from it.)
        if lang in STACK_DETAIL_QUESTIONS and not self.data.get("detail_prefilled"):
            steps.append(WIZARD_STACK_DETAIL)
        if lang not in NO_GOALS_LANGS:
            steps += [WIZARD_GOALS, WIZARD_RULES]
        return steps

    def _hdr(self, state: str) -> str:
        flow = self._flow()
        return (f"**Step {flow.index(state) + 1} of {len(flow)}**"
                if state in flow else "**Next**")

    def _advance(self, done: str) -> str:
        """Move past `done` and return the next question (or the summary)."""
        flow = self._flow()
        nxt = flow[flow.index(done) + 1] if done in flow \
            and flow.index(done) + 1 < len(flow) else WIZARD_CONFIRM
        self.state = nxt
        if nxt == WIZARD_CONFIRM:
            return self._summary()
        if nxt == WIZARD_STACK_DETAIL:
            return f"{self._hdr(nxt)} — {STACK_DETAIL_QUESTIONS[self._lang()]}"
        return f"{self._hdr(nxt)} — {STEP_PROMPTS[nxt]}"

    def start(self) -> str:
        self.reset()
        self.state = WIZARD_NAME
        self.log.info("Wizard started")
        return (
            "🚀 **New Project Wizard**\n\n"
            "Four to six quick questions, then I build the project and open a "
            "session on it.\nType `cancel` at any point to stop.\n\n"
            "**Step 1** — What's the project name?\n"
            "*(lowercase, hyphens OK — e.g. `my-api`, `portfolio-site`)*"
        )

    async def step(self, message: str) -> str:
        text = message.strip()

        if text.lower() == "cancel":
            return self.cancel()

        if self.state == WIZARD_NAME:
            name, err = self.check_name(text)
            if err:
                return err
            self.data["name"] = name
            self.state = WIZARD_DESCRIPTION
            return (
                f"Got it — **{name}**.\n\n"
                "**Step 2** — Describe what this project does.\n"
                "*(A sentence or two — goes into CLAUDE.md and PLAN.md)*"
            )

        if self.state == WIZARD_DESCRIPTION:
            self.data["description"] = text
            self.state = WIZARD_STACK
            return f"**Step 3** — {STACK_MENU}"

        if self.state == WIZARD_STACK:
            key, prefill = resolve_stack_input(text)
            if key:
                self.data["stack_key"] = key
                label = STACKS[key][0]
                if prefill:
                    # They named a runtime, which answers the row's follow-up.
                    self.data["stack_detail"] = prefill
                    self.data["detail_prefilled"] = True
                    label = f"{label} · {prefill}"
            else:
                self.data["stack_key"]    = "0"
                self.data["custom_stack"] = text
                label = text
            return f"Type: **{label}** ✓\n\n" + self._advance(WIZARD_STACK)

        if self.state == WIZARD_STACK_DETAIL:
            self.set_detail("" if text.lower() == "skip" else text)
            return self._advance(WIZARD_STACK_DETAIL)

        if self.state == WIZARD_GOALS:
            self.data["goals"] = "" if text.lower() == "skip" else text
            return self._advance(WIZARD_GOALS)

        if self.state == WIZARD_RULES:
            self.data["rules"] = "" if text.lower() == "skip" else text
            return self._advance(WIZARD_RULES)

        if self.state == WIZARD_CONFIRM:
            if text.lower() in ("yes", "y", "confirm", "go", "do it", "ok", "yep", "yeah"):
                return await self._execute()
            elif text.lower() in ("no", "n", "cancel", "abort"):
                return self.cancel()
            return "Reply **yes** to create the project or **no** to cancel."

        return "Something went wrong — type `cancel` to reset."

    def check_name(self, text: str) -> tuple:
        """(clean name, "") or ("", why it can't be used)."""
        name = re.sub(r"[^a-z0-9\-_]", "", (text or "").lower().replace(" ", "-"))
        if not name:
            return "", "That name didn't work — use letters, numbers, hyphens, or underscores."
        if name in RESERVED_NAMES:
            return "", (f"`{name}` is reserved (a bot session or bot folder uses it). "
                        "Pick another name.")
        if (Path(self.cfg["projects_root"]) / name).exists():
            return "", f"A project named `{name}` already exists. Try a different name."
        return name, ""

    def set_detail(self, detail: str):
        """Record the type's follow-up answer. A Game Boy CTF ROM is a GBDK
        project with the GBC-CTF playbook, not a separate scaffold, so it's
        routed onto the path that has one."""
        self.data["stack_detail"] = detail
        if self._lang() == "CTFDEV" and re.search(
                r"\bgb\b|game ?boy|gbc|gbdk", detail.lower()):
            self.data["stack_key"] = "7"
            self.data["stack_detail"] = f"ctf, {detail}"

    def _summary(self, confirm_hint: bool = True) -> str:
        name   = self.data["name"]
        desc   = self.data.get("description", "")
        lang   = self._lang()
        label  = self.data.get("custom_stack") or STACKS.get(
            self.data.get("stack_key", "0"), STACKS["0"])[0]
        detail = self.data.get("stack_detail", "").strip()
        path   = Path(self.cfg["projects_root"]) / name
        lt     = launch_type_for(lang)

        def trim(s, n): return s[:n] + "…" if len(s) > n else s

        rows = ["├── CLAUDE.md   ← project context & goals",
                "├── RULES.md    ← constraints & coding standards",
                "├── PLAN.md     ← phased development plan",
                "├── SKILLS.md   ← reusable prompt patterns",
                "├── LOG.md      ← change journal (updated every prompt)",
                "├── tools/      ← project scripts (setup.sh for anything root)"]
        if lang == "CTF":
            rows = ["├── CLAUDE.md   ← challenge context & methodology",
                    "├── RULES.md    ← CTF workflow rules",
                    "├── PLAN.md     ← enumerate → analyze → solve",
                    "├── SKILLS.md   ← prompt patterns for this category",
                    "├── LOG.md      ← what's been tried and found",
                    "├── tools/      ← solve scripts (setup.sh for anything root)",
                    "├── notes.md    ← drop clue data here",
                    "├── assets/     ← drop challenge files here"]
        if lang == "RE":
            rows += ["├── target/     ← drop the binary / .ipa / .apk here",
                     "├── analysis/   ← every tool's output lands here as a file"]
        if lt != "none":
            rows += ["├── LAUNCH.md   ← launch.sh rules for Claude Code",
                     "├── launch.sh   ← standardized run script"]
        rows[-1] = "└" + rows[-1][1:]
        tree = f"```\n{path}/\n" + "\n".join(rows) + "\n```\n\n"

        out = ["**Here's what I'll create:**\n", tree,
               f"**Name:** `{name}`\n**Type:** {label}\n"]
        if detail:
            out.append(f"**Detail:** {trim(detail, 120)}\n")
        if lang in ("Web", "Script", "Native"):
            rl, rf, _t, _r = resolve_runtime(lang, detail)
            out.append(f"**Stack:** {rl}{' + ' + rf if rf else ''}\n")
        out.append(f"**Description:** {trim(desc, 120)}\n")
        if lang not in NO_GOALS_LANGS:
            out.append(f"**Goals:** {trim(self.data.get('goals') or '(none)', 100)}\n")
            out.append(f"**Rules:** {trim(self.data.get('rules') or '(none)', 80)}\n")
        if lang == "RE":
            out.append("**Tooling:** the REA harness gets registered for this project "
                       "(MCP server `rea`, Ghidra 12.1.4 + JDK 21)\n")
        out.append("\nA `claude remote-control` session will be spawned automatically.")
        if confirm_hint:
            out.append("\n\nType **yes** to create, **no** to cancel.")
        return "".join(out)


    async def _execute(self) -> str:
        name = self.data["name"]
        path = Path(self.cfg["projects_root"]) / name
        # Inferred, not asked (see the note at the top of the wizard).
        self.data["launch_type"] = launch_type_for(self._lang())

        # Web projects get a unique port from the persistent counter.
        if self.data.get("launch_type") == "web":
            port = int(self.cfg.get("next_web_port", 8100))
            self.data["port"] = port
            self.cfg["next_web_port"] = port + 1
            try:
                save_config(self.cfg)
            except OSError as e:
                self.log.warning(f"Could not persist next_web_port: {e}")

        try:
            path.mkdir(parents=True)
            self.log.info(f"Created project directory: {path}")
            try:
                gr = subprocess.run(["git", "init"], cwd=path, capture_output=True)
                if gr.returncode != 0:
                    self.log.warning(f"git init failed for {name}: {gr.stderr.decode().strip()}")
                else:
                    self.log.info(f"git init OK for {name}")
            except FileNotFoundError:
                self.log.warning(f"git not found — skipping git init for {name}")
            write_project_files(self.cfg, self.data)
            self.log.info(f"Project files written for {name}")
        except Exception as e:
            self.reset()
            return f"❌ Failed to create project files: {e}"

        mcp = {"rea": rea_mcp_server()} if self._lang() == "RE" else None
        trust_workspace(str(path), self.log, mcp_servers=mcp)
        if mcp:
            self.log.info(f"Registered REA MCP for {name}: {mcp['rea']['command']} "
                          f"env={sorted(mcp['rea']['env'])}")
        self.log.info(f"Spawning session for {name}...")
        ok, msg = await spawn_session(self.cfg, name)
        self.reset()

        if ok:
            self.log.info(f"Session spawned successfully for {name}")
            return (
                f"✅ **{name}** is ready!\n\n"
                "**Files created:**\n"
                "- `CLAUDE.md` — project context and goals\n"
                "- `RULES.md` — constraints and coding standards\n"
                "- `PLAN.md` — phased development plan\n"
                "- `SKILLS.md` — reusable prompt patterns\n"
                "- `LOG.md` — change journal (Claude appends after every prompt)\n"
                "- `tools/setup.sh` — anything needing root goes here, then "
                f"`/sudo action:approve project:{name} script:tools/setup.sh`\n"
                + ("- `LAUNCH.md` — launch.sh rules for Claude Code\n"
                   "- `launch.sh` — standardized run script\n" if self.data.get("launch_type", "none") != "none" else "")
                + "\n"
                + msg
            )
        self.log.error(f"Session spawn failed for {name}: {msg}")
        return f"✅ Project files created but session failed to start:\n{msg}"


# ── /new as a tap-through card (2026-10-29) ──────────────────────────────────
# Same UX as ProtoMan's part cards (protoman/cards.py), which the owner found far
# easier on a phone than a typed Q&A: the name and type come in as slash
# options; then one private card shows exactly what will be created, with a
# dropdown for every choice, one pop-up (pre-filled) for everything typed, and
# Create / Cancel. The ProjectWizard object still holds the data and does the
# work (_summary, set_detail, _execute), so both paths build identical projects.
#
# Components carry custom_id "np|<verb>|<user id>[|arg]". The half-filled form
# lives in CommandHandler._new_forms; after a bot restart a tap says so and
# asks for /new again (nothing has been created at that point).

NP = "np"

# Per type: a dropdown of the usual answers to its follow-up question.
# (label, detail text stored; the first entry is the default.) Values are
# words resolve_runtime() understands, so the stack resolves the same way a
# typed answer did.
NEW_PICKS = {
    "Web":      ("Language / framework", [
        ("Python + FastAPI", "python fastapi"), ("Python + Flask", "python flask"),
        ("Python + Django", "python django"), ("Next.js", "nextjs"),
        ("Node + Express", "node express"), ("React / Vite", "vite"),
        ("Node (plain)", "node")]),
    "Script":   ("Language", [
        ("Python", "python"), ("Node", "node"), ("Bash", "bash"),
        ("Rust", "rust"), ("Go", "go")]),
    "Native":   ("Language", [
        ("Rust", "rust"), ("C / C++", "c++"), ("Go", "go")]),
    "CTF":      ("Category", [
        ("Misc / unknown", "misc"), ("Reverse engineering", "re"),
        ("Binary exploitation (pwn)", "pwn"), ("Crypto", "crypto"),
        ("Web", "web"), ("Forensics", "forensics")]),
    "CTFDEV":   ("Category", [
        ("Game Boy / GBC ROM", "gb"), ("Reverse engineering", "re"),
        ("Binary exploitation (pwn)", "pwn"), ("Crypto", "crypto"),
        ("Web", "web"), ("Forensics", "forensics"), ("Misc", "misc")]),
    "GBDK":     ("Kind of ROM", [
        ("Game (DMG-first)", "game"), ("GBC CTF challenge ROM", "ctf")]),
    "OpenSCAD": ("Material", [
        ("PLA", "PLA"), ("PETG", "PETG"), ("ASA", "ASA"), ("TPU", "TPU")]),
}

# Per type: a free-text box in the Details pop-up, for what a dropdown can't
# hold. (box label ≤45 chars, placeholder.)
NEW_NOTES = {
    "RE":     ("Target + what you want out of it",
               "e.g. Android .apk — understand the licence check"),
    "KiCad":  ("Board constraints",
               "e.g. 2-layer, JLCPCB, USB-C powered"),
    "CTFDEV": ("Difficulty / intended solve path (optional)",
               "e.g. medium; XOR'd flag in WRAM"),
    "GBDK":   ("Cartridge details (optional)", "e.g. MBC5, Color-first"),
    "Native": ("What is it? (optional)", "e.g. indexing daemon"),
    "":       ("What kind of project? (optional)", "e.g. home-automation glue"),
}


def np_cid(verb: str, uid, *args) -> str:
    return "|".join([NP, verb, str(uid), *[str(a) for a in args]])


def np_parse(custom_id: str):
    """(verb, user id, [args]) for a /new component, else None."""
    parts = (custom_id or "").split("|")
    if len(parts) < 3 or parts[0] != NP or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2]), parts[3:]


def new_form_compose(wiz) -> None:
    """Fold the picked option + typed note into the wizard's detail text."""
    lang = wiz._lang()
    pick = wiz.data.get("np_pick", "")
    note = wiz.data.get("np_note", "").strip()
    if lang == "" and note:
        wiz.data["custom_stack"] = note
        wiz.data["stack_detail"] = ""
        return
    wiz.data.pop("custom_stack", None)
    wiz.data["stack_key"] = wiz.data.get("np_key", wiz.data.get("stack_key", "0"))
    wiz.set_detail(", ".join(x for x in (pick, note) if x))


def new_form_text(wiz) -> str:
    new_form_compose(wiz)
    desc = wiz.data.get("description", "").strip()
    head = (f"🚀 **New project: `{wiz.data['name']}`**\n"
            "Check it over. Change the type or its options with the dropdowns, "
            "and tap **📝 Details** to type the description"
            + ("" if wiz._lang() in NO_GOALS_LANGS else ", goals and rules")
            + ". Nothing is created until **✅ Create**.\n")
    if not desc:
        head += "⚠️ No description yet; it goes into CLAUDE.md and PLAN.md.\n"
    text = head + "\n" + wiz._summary(confirm_hint=False)
    return text if len(text) <= 1990 else text[:1980] + "…"


def new_form_view(uid, wiz):
    """Type dropdown, the type's option dropdown, Details / Create / Cancel."""
    import discord
    v = discord.ui.View(timeout=None)
    key = wiz.data.get("np_key", "0")
    v.add_item(discord.ui.Select(
        custom_id=np_cid("type", uid), row=0, placeholder="Project type",
        options=[discord.SelectOption(label=lbl[:100], value=k, default=(k == key))
                 for k, (lbl, *_rest) in sorted(STACKS.items(),
                                                 key=lambda kv: (kv[0] == "0", kv[0]))]))
    picks = NEW_PICKS.get(wiz._lang())
    if picks:
        title, opts = picks
        cur = wiz.data.get("np_pick") or opts[0][1]
        v.add_item(discord.ui.Select(
            custom_id=np_cid("pick", uid), row=1, placeholder=title,
            options=[discord.SelectOption(label=lbl[:100], value=val,
                                          default=(val == cur)) for lbl, val in opts]))
    v.add_item(discord.ui.Button(label="Details", emoji="📝", row=2,
                                 style=discord.ButtonStyle.primary,
                                 custom_id=np_cid("details", uid)))
    v.add_item(discord.ui.Button(label="Create", emoji="✅", row=2,
                                 style=discord.ButtonStyle.success,
                                 custom_id=np_cid("create", uid)))
    v.add_item(discord.ui.Button(label="Cancel", emoji="✖️", row=2,
                                 style=discord.ButtonStyle.secondary,
                                 custom_id=np_cid("cancel", uid)))
    v.stop()   # routed by on_interaction, not discord.py's view store
    return v


def new_form_modal(uid, wiz):
    """One pop-up for everything typed, pre-filled with what's there."""
    import discord
    m = discord.ui.Modal(title="Project details", custom_id=np_cid("modal", uid),
                         timeout=None)
    d = wiz.data
    m.add_item(discord.ui.TextInput(label="Name", custom_id="name",
                                    default=d.get("name", ""), max_length=60))
    m.add_item(discord.ui.TextInput(
        label="What does it do?", custom_id="description", required=False,
        style=discord.TextStyle.paragraph, max_length=1000,
        default=d.get("description") or None,
        placeholder="A sentence or two; goes into CLAUDE.md and PLAN.md"))
    note = NEW_NOTES.get(wiz._lang())
    if note:
        m.add_item(discord.ui.TextInput(
            label=note[0][:45], custom_id="note", required=False, max_length=300,
            default=d.get("np_note") or None, placeholder=note[1][:100]))
    if wiz._lang() not in NO_GOALS_LANGS:
        m.add_item(discord.ui.TextInput(
            label="Goals (one per line)", custom_id="goals", required=False,
            style=discord.TextStyle.paragraph, max_length=1000,
            default=d.get("goals") or None, placeholder="These become CLAUDE.md goals"))
        m.add_item(discord.ui.TextInput(
            label="Hard rules (optional)", custom_id="rules", required=False,
            style=discord.TextStyle.paragraph, max_length=600,
            default=d.get("rules") or None,
            placeholder="e.g. no external API calls; mobile-first UI"))
    m.stop()
    return m


def modal_values(data: dict) -> dict:
    """{custom_id: value} from a modal submit, whatever the nesting."""
    out = {}

    def walk(node):
        if isinstance(node, dict):
            if "custom_id" in node and "value" in node:
                out[node["custom_id"]] = node["value"]
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk((data or {}).get("components", []))
    return out


# ── Box builder (/box) ────────────────────────────────────────────────────────
#
# Front-end to the vendored box-builder tool (box-builder/, engine is
# smkent/monoscad rugged-box, CC BY-SA 4.0). We drive its documented bot
# interface — `box_wizard.py --json` — rather than importing its Python API, so
# a crash or hang in the renderer stays in its own process behind a timeout.

BOX_ROOT    = Path(__file__).resolve().parent / "box-builder"
BOX_CLI     = BOX_ROOT / "scripts" / "box_wizard.py"
BOX_TIMEOUT = 420            # a `draw` latch adds ~20s; 5 min ceiling + slack

# Steps the wizard asks outright, in order. Everything else keeps its default
# until the user unlocks it with `advance` at the confirm step. Defaults are
# repeated here (not imported from rbox_common) so the bot still runs if
# box-builder is missing — every value is passed to the CLI explicitly, so the
# bot's value always wins and the two can't silently drift.
BOX_STEPS = [
    dict(key="Width", kind="int", lo=20, hi=300, default=180,
         prompt="Interior **width** — side-to-side, mm", hint="20–300"),
    dict(key="Length", kind="int", lo=20, hi=300, default=90,
         prompt="Interior **length** — front-to-back, mm", hint="20–300"),
    dict(key="Bottom_Height", kind="int", lo=0, hi=300, default=40,
         prompt="Interior **base** depth, mm", hint="0–300"),
    dict(key="Top_Height", kind="int", lo=0, hi=300, default=20,
         prompt="Interior **lid** depth, mm", hint="0–300"),
    # The engine takes 0 to mean "auto" and resolves it to 1 or 2 — it can't
    # build a clasp-less box. Showing a literal "0" read as "no clasps", so the
    # wizard says `auto` and translates it on the way out (BOX_LATCH_AUTO).
    dict(key="Latch_Count", kind="choice", choices=["auto", "1", "2"], default="2",
         prompt="How many **clasps**?",
         hint="`auto` lets the engine pick 1 or 2 from the box size"),
    dict(key="Latch_Type", kind="choice", choices=["clip", "draw"], default="clip",
         prompt="**Clasp style**", hint="`draw` looks meaner, adds ~20s render"),
    dict(key="Handle", kind="bool", default=False,
         prompt="Add a **carry handle**?", hint="needs a 2-clasp box"),
    dict(key="Label", kind="bool", default=False,
         prompt="Add a **label plate**?", hint="needs a 2-clasp box"),
    dict(key="Label_Text", kind="str", default="Label", only_if="Label",
         prompt="**Label text**", hint="max ~20 chars"),
]

# Unlocked with `advance`; picked one at a time as `<n> <value>`.
BOX_ADVANCED = [
    dict(key="Lip_Seal_Type", kind="choice", default="wedge",
         choices=["none", "wedge", "square", "filament-1.75mm"], label="Lip seal"),
    dict(key="Corner_Radius", kind="float", lo=0, hi=20, default=5.0,
         label="Corner radius (mm)"),
    dict(key="Reinforced_Corners", kind="bool", default=False,
         label="Reinforced corners"),
    dict(key="Top_Grip", kind="bool", default=False,
         label="Lid front grip (2-clasp only)"),
    dict(key="Wall_Thickness", kind="float", lo=0.4, hi=10, default=3.0,
         label="Wall thickness (mm)"),
    dict(key="Lip_Thickness", kind="float", lo=0.4, hi=10, default=3.0,
         label="Lip thickness (mm)"),
]

BOX_DEFAULTS = {o["key"]: o["default"] for o in BOX_STEPS + BOX_ADVANCED}

# What the wizard shows for the engine's "0 = auto" latch count.
BOX_LATCH_AUTO = "auto"

# Part colours for the renders — base/lid two-tone, hardware in slate.
BOX_COLORS = {"bottom": "#e07a2f", "top": "#7ab8f0",
              "latch": "#546074", "handle": "#546074", "label": "#c8d0dc"}


def _box_fmt(v) -> str:
    return {True: "yes", False: "no"}.get(v, str(v))


def _box_parse(opt: dict, text: str):
    """Parse one wizard answer. Returns (value, None) or (None, error_string)."""
    t = text.strip()
    kind = opt["kind"]
    if kind == "bool":
        if t.lower() in ("y", "yes", "true", "1", "yep", "yeah"):
            return True, None
        if t.lower() in ("n", "no", "false", "0", "nope"):
            return False, None
        return None, "Reply **yes** or **no**."
    if kind == "choice":
        if t.lower() in [c.lower() for c in opt["choices"]]:
            return next(c for c in opt["choices"] if c.lower() == t.lower()), None
        return None, "Choose one of: " + ", ".join(f"`{c}`" for c in opt["choices"])
    if kind in ("int", "float"):
        try:
            v = int(t) if kind == "int" else float(t)
        except ValueError:
            return None, f"That's not a number — enter a value in {opt['hint']}."
        if not (opt["lo"] <= v <= opt["hi"]):
            return None, f"Out of range — must be {opt['lo']}–{opt['hi']}."
        return v, None
    if not t:
        return None, "That can't be empty."
    return t, None


def _box_cli_args(params: dict) -> list:
    """Turn the param dict into box_wizard.py flags (bools use --x/--no-x)."""
    args = []
    for key, val in params.items():
        flag = "--" + key.lower().replace("_", "-")
        if isinstance(val, bool):
            args.append(flag if val else "--no-" + key.lower().replace("_", "-"))
        else:
            args += [flag, str(val)]
    return args


def _box_autocrop(path: Path, margin: int = 28):
    """Trim the flat background OpenSCAD leaves around the model.

    `--viewall` fits the scene's bounding sphere with a wide safety margin, so
    a raw render is mostly empty. Cropping to the pixels that differ from the
    corner colour gives a tight frame at any box size, which matters on a phone.
    """
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return
    try:
        with Image.open(path) as src:
            im = src.convert("RGB")
        bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
        bbox = ImageChops.difference(im, bg).getbbox()
        if not bbox:
            return
        left, top, right, bottom = bbox
        im.crop((max(0, left - margin), max(0, top - margin),
                 min(im.width, right + margin),
                 min(im.height, bottom + margin))).save(path)
    except Exception:  # noqa: BLE001 — cropping is cosmetic, keep the render
        pass


def _box_hinge_axis(mesh, samples: int = 14):
    """Locate the hinge screw-bore axis: the round void running along X on the
    +Y (hinge) side of a part. Returns z of the axis, or None.

    Both halves carry their hinge at the same +Y, so aligning these two z's is
    what seats the lid on the base. Stacking by bounding box instead leaves the
    lid floating ~9mm high, which reads as a broken hinge in the render.
    """
    import numpy as np
    from shapely.geometry import Polygon

    x0, x1 = mesh.bounds[0][0], mesh.bounds[1][0]
    y_max = mesh.bounds[1][1]
    hits = []
    for x in np.linspace(x0 + 2, x1 - 2, samples):
        try:
            sec = mesh.section(plane_origin=[x, 0, 0], plane_normal=[1, 0, 0])
            if sec is None:
                continue
            planar, to_3d = sec.to_planar()
        except Exception:  # noqa: BLE001 — degenerate slice, just skip it
            continue
        for poly in planar.polygons_full:
            for ring in poly.interiors:          # bores are interior rings
                g = Polygon(ring)
                if g.area <= 0 or g.length <= 0:
                    continue
                r = math.sqrt(g.area / math.pi)
                if not (0.8 <= r <= 3.0):        # M3 clearance ≈ 1.45–1.8mm
                    continue
                if 4 * math.pi * g.area / g.length ** 2 < 0.85:
                    continue                     # not round — a slot or rib gap
                c = g.centroid
                w = to_3d @ np.array([c.x, c.y, 0.0, 1.0])
                if w[1] > y_max * 0.7:           # hinge side, not the clasp side
                    hits.append(w[2])
    return float(np.median(hits)) if hits else None


def box_render_shots(files: dict, dest: Path, logger=None) -> tuple:
    """Render the two result images. Returns (png_paths, warnings).

    Shot 1 — the assembled box twice side by side, the right copy yawed 180°,
             so one image shows both the hinge face and the clasp face.
    Shot 2 — every generated part laid out flat, as it goes to the slicer.
    """
    warns, shots = [], []
    if not shutil.which("openscad"):
        return [], ["`openscad` not installed — skipped the renders."]
    try:
        import trimesh
    except ImportError:
        return [], ["`trimesh` not installed — skipped the renders."]

    meshes, bounds = {}, {}
    for part, path in files.items():
        try:
            m = trimesh.load(path)
            meshes[part], bounds[part] = m, m.bounds
        except Exception as e:  # noqa: BLE001 — a bad STL shouldn't kill the build
            warns.append(f"could not read `{Path(path).name}`: {e}")
    if "bottom" not in bounds or "top" not in bounds:
        return [], warns + ["missing base/lid STL — skipped the renders."]

    def imp(part, color=None):
        return (f'color("{color or BOX_COLORS.get(part, "#888888")}") '
                f'import("{files[part]}", convexity=10);')

    # Seat the lid by matching hinge axes; fall back to a rim-to-rim stack.
    hz_base, hz_lid = _box_hinge_axis(meshes["bottom"]), _box_hinge_axis(meshes["top"])
    if hz_base is not None and hz_lid is not None:
        dz = hz_base + hz_lid          # lid is flipped, so its z negates
    else:
        dz = ((bounds["bottom"][1][2] - bounds["bottom"][0][2])
              + (bounds["top"][1][2] - bounds["top"][0][2]))
        warns.append("couldn't find the hinge axis — lid placed by bounding box.")
        if logger:
            logger.warning("box render: hinge axis not found, used bbox stacking")

    assembled = f'{imp("bottom")}\ntranslate([0,0,{dz:.3f}]) rotate([0,180,0]) {imp("top")}'
    width = bounds["bottom"][1][0] - bounds["bottom"][0][0]
    gap = max(12.0, width * 0.12)
    both_sides = (f"translate([{-(width + gap) / 2:.2f},0,0]) {{{assembled}}}\n"
                  f"translate([{(width + gap) / 2:.2f},0,0]) rotate([0,0,180]) "
                  f"{{{assembled}}}")

    # Parts laid flat as they'd go on the bed. Packed into rows rather than one
    # long line, so the shot isn't a thin strip with the parts too small to see.
    order = [p for p in ("bottom", "top", "latch", "handle", "label") if p in bounds]
    size = {p: (bounds[p][1][0] - bounds[p][0][0],
                bounds[p][1][1] - bounds[p][0][1]) for p in order}
    pad = 14.0
    row_limit = sum(size[p][0] for p in order[:2]) + pad   # base + lid on row 1
    rows, cur, cur_w = [], [], 0.0
    for p in order:
        w = size[p][0]
        if cur and cur_w + pad + w > row_limit * 1.05:
            rows.append(cur); cur, cur_w = [], 0.0
        cur_w += (pad if cur else 0) + w
        cur.append(p)
    if cur:
        rows.append(cur)

    laid, y = [], 0.0
    total_w = 0.0
    for row in rows:
        row_w = sum(size[p][0] for p in row) + pad * (len(row) - 1)
        row_d = max(size[p][1] for p in row)
        total_w = max(total_w, row_w)
        x = -row_w / 2
        for p in row:
            b = bounds[p]
            laid.append(f'translate([{x - b[0][0]:.2f},'
                        f'{y - b[0][1] - row_d / 2:.2f},{-b[0][2]:.2f}]) ' + imp(p))
            x += size[p][0] + pad
        y -= row_d + pad
    total_d = -y
    parts = (f"translate([0,{total_d / 2 - pad / 2:.2f},0]) {{\n"
             + "\n".join(laid) + "\n}")

    # Frame each shot to its own scene: a 2-up assembled view is wide, the
    # packed parts grid is closer to 16:9. --viewall fits the scene's extent,
    # so a mismatched canvas just pads the result with empty background.
    def canvas(w_mm, d_mm, h_mm, rx):
        proj_h = d_mm * math.cos(math.radians(rx)) + h_mm * math.sin(math.radians(rx))
        # --viewall fits the bounding sphere with a wide margin, so the model
        # covers roughly 40% of the canvas — render ~3x the target size and let
        # _box_autocrop() bring it back to a crisp, tightly framed image.
        aspect = max(0.9, min(3.2, w_mm / max(proj_h, 1e-6)))
        height = int(max(1100, min(2000, 3200 / aspect)))
        return f"3200,{height}"

    asm_h = dz + (bounds["top"][1][2] - bounds["top"][0][2])
    shot_specs = (
        ("assembled", both_sides, "0,0,0,62,0,25,0",
         canvas(2 * width + gap, bounds["bottom"][1][1] - bounds["bottom"][0][1],
                asm_h, 62)),
        ("parts", parts, "0,0,0,58,0,18,0",
         canvas(total_w, total_d, max(bounds[p][1][2] - bounds[p][0][2]
                                      for p in order), 58)),
    )
    for name, src, cam, imgsize in shot_specs:
        png = dest / f"render-{name}.png"
        scad = dest / f".{name}.scad"
        try:
            scad.write_text(src)
            cmd = ["openscad", "-o", str(png), f"--imgsize={imgsize}",
                   "--viewall", "--autocenter", "--projection=ortho",
                   f"--camera={cam}", "--colorscheme=Tomorrow", str(scad)]
            if shutil.which("xvfb-run"):     # works headless / logged out
                cmd = ["xvfb-run", "-a"] + cmd
            env = {**os.environ}
            if not shutil.which("xvfb-run") and not env.get("DISPLAY"):
                env["DISPLAY"] = ":0"
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                               env=env)
            if png.exists():
                _box_autocrop(png)
                shots.append(png)
            else:
                tail = ((r.stderr or "").strip().splitlines() or ["(no output)"])[-3:]
                warns.append(f"{name} render failed: " + " ".join(tail))
        except Exception as e:  # noqa: BLE001 — renders are a bonus, not the build
            warns.append(f"{name} render failed: {e}")
        finally:
            scad.unlink(missing_ok=True)
    return shots, warns


class BoxWizard:
    """`/box` — asks the common options, hides the rest behind `advance`, then
    generates + verifies a rugged box and posts the renders."""

    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg   = cfg
        self.log   = logger
        self.state = WIZARD_IDLE
        self.idx   = 0
        self.name  = ""
        self.params = dict(BOX_DEFAULTS)

    def reset(self):
        self.state = WIZARD_IDLE
        self.idx = 0
        self.name = ""
        self.params = dict(BOX_DEFAULTS)

    def is_active(self) -> bool:
        return self.state != WIZARD_IDLE

    def cancel(self) -> str:
        self.log.info("Box wizard cancelled")
        self.reset()
        return "❌ Box wizard cancelled. Type `/box` to start again."

    def boxes_root(self) -> Path:
        return Path(self.cfg["projects_root"]) / BOXES_DIRNAME

    def start(self) -> str:
        self.reset()
        self.state = "name"
        self.log.info("Box wizard started")
        return (
            "📦 **Rugged Box Builder**\n\n"
            "I'll ask 9 quick questions, then generate and printability-test the "
            "parts. Press `-` to accept a default, `cancel` to stop.\n\n"
            "**Name** — what should I call this box?\n"
            "*(lowercase, hyphens OK — becomes the output folder)*"
        )

    # ── step plumbing ─────────────────────────────────────────────────────
    def _visible_steps(self) -> list:
        """Steps minus the ones gated off by an earlier answer (Label_Text)."""
        return [s for s in BOX_STEPS
                if not s.get("only_if") or self.params.get(s["only_if"])]

    def _ask(self, opt: dict, n: int, total: int) -> str:
        cur = self.params[opt["key"]]
        hint = f"\n*({opt['hint']})*" if opt.get("hint") else ""
        return (f"**Step {n} of {total}** — {opt['prompt']}{hint}\n"
                f"Default: `{_box_fmt(cur)}` — reply `-` to keep it.")

    def _next_question(self) -> str:
        steps = self._visible_steps()
        if self.idx >= len(steps):
            self.state = "confirm"
            return self._summary()
        return self._ask(steps[self.idx], self.idx + 1, len(steps))

    async def step(self, message: str):
        text = message.strip()
        if text.lower() == "cancel":
            return self.cancel()

        if self.state == "name":
            name = re.sub(r"[^a-z0-9\-_]", "", text.lower().replace(" ", "-"))
            if not name:
                return "That name didn't work — letters, numbers, hyphens or underscores."
            if (self.boxes_root() / name).exists():
                return (f"A box called `{name}` already exists. Pick another name "
                        f"(or `/box` again after deleting it).")
            self.name = name
            self.state = "steps"
            return f"Got it — **{name}**.\n\n" + self._next_question()

        if self.state == "steps":
            steps = self._visible_steps()
            opt = steps[self.idx]
            if text != "-":
                val, err = _box_parse(opt, text)
                if err:
                    return err
                self.params[opt["key"]] = val
            self.idx += 1
            return self._next_question()

        if self.state == "advance":
            return self._advance_step(text)

        if self.state == "edit":
            return self._edit_step(text)

        if self.state == "confirm":
            low = text.lower()
            if low in ("go", "yes", "y", "build", "ok", "yep", "yeah", "do it"):
                return await self._execute()
            if low in ("advance", "advanced", "adv", "more"):
                self.state = "advance"
                return self._advanced_table()
            if low in ("edit", "back", "change", "revise"):
                self.state = "edit"
                return self._edit_table()
            if low in ("no", "n", "cancel", "abort"):
                return self.cancel()
            return ("Reply **go** to build, **edit** to change an answer, "
                    "**advance** for more options, or **cancel**.")

        return "Something went wrong — type `cancel` to reset."

    # ── advanced options ──────────────────────────────────────────────────
    def _advanced_table(self, note: str = "") -> str:
        rows = []
        for i, o in enumerate(BOX_ADVANCED, 1):
            cur = _box_fmt(self.params[o["key"]])
            if o["kind"] == "choice":
                allowed = " / ".join(o["choices"])
            elif o["kind"] == "bool":
                allowed = "yes / no"
            else:
                allowed = f"{o['lo']}–{o['hi']}"
            rows.append(f"{i:>2}  {o['label']:<28} {cur:<10} {allowed}")
        return (
            (note + "\n\n" if note else "")
            + "⚙️ **Advanced options**\n```\n"
            + f"{'#':>2}  {'option':<28} {'current':<10} allowed\n"
            + "\n".join(rows)
            + "\n```\n"
            "Reply `<number> <value>` to change one *(e.g. `1 square` or `5 2.4`)*.\n"
            "Reply `done` when finished, or `cancel` to abort."
        )

    def _advance_step(self, text: str) -> str:
        low = text.lower()
        if low in ("done", "back", "go", "ok"):
            self.state = "confirm"
            return self._summary()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return self._advanced_table("❌ Use `<number> <value>`, e.g. `1 square`.")
        n = int(parts[0])
        if not 1 <= n <= len(BOX_ADVANCED):
            return self._advanced_table(f"❌ Pick a number 1–{len(BOX_ADVANCED)}.")
        opt = BOX_ADVANCED[n - 1]
        val, err = _box_parse(opt, parts[1])
        if err:
            return self._advanced_table(f"❌ {opt['label']}: {err}")
        self.params[opt["key"]] = val
        return self._advanced_table(f"✅ {opt['label']} → `{_box_fmt(val)}`")

    # ── going back to change an answer ────────────────────────────────────
    def _edit_rows(self) -> list:
        """Editable rows: the name, then whichever main steps are in play."""
        return [None] + list(self._visible_steps())   # None == the box name

    def _edit_table(self, note: str = "") -> str:
        rows = []
        for i, o in enumerate(self._edit_rows(), 1):
            if o is None:
                rows.append(f"{i:>2}  {'Name':<22} {self.name:<12} text")
                continue
            if o["kind"] == "choice":
                allowed = " / ".join(o["choices"])
            elif o["kind"] == "bool":
                allowed = "yes / no"
            elif o["kind"] == "str":
                allowed = "text"
            else:
                allowed = f"{o['lo']}–{o['hi']}"
            label = o["key"].replace("_", " ")
            rows.append(f"{i:>2}  {label:<22} "
                        f"{_box_fmt(self.params[o['key']]):<12} {allowed}")
        return (
            (note + "\n\n" if note else "")
            + "✏️ **Your answers**\n```\n"
            + f"{'#':>2}  {'question':<22} {'current':<12} allowed\n"
            + "\n".join(rows)
            + "\n```\n"
            "Reply `<number> <value>` to change one *(e.g. `5 2` or `1 tool-case`)*.\n"
            "Reply `done` to go back to the summary, or `cancel` to abort."
        )

    def _edit_step(self, text: str) -> str:
        low = text.lower()
        if low in ("done", "back", "go", "ok"):
            self.state = "confirm"
            return self._summary()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return self._edit_table("❌ Use `<number> <value>`, e.g. `5 2`.")
        rows = self._edit_rows()
        n = int(parts[0])
        if not 1 <= n <= len(rows):
            return self._edit_table(f"❌ Pick a number 1–{len(rows)}.")
        opt = rows[n - 1]

        if opt is None:                                  # renaming the box
            name = re.sub(r"[^a-z0-9\-_]", "", parts[1].lower().replace(" ", "-"))
            if not name:
                return self._edit_table("❌ Name: letters, numbers, hyphens or "
                                        "underscores.")
            if name != self.name and (self.boxes_root() / name).exists():
                return self._edit_table(f"❌ A box called `{name}` already exists.")
            self.name = name
            return self._edit_table(f"✅ Name → `{name}`")

        val, err = _box_parse(opt, parts[1])
        if err:
            return self._edit_table(f"❌ {opt['key'].replace('_', ' ')}: {err}")
        self.params[opt["key"]] = val
        # Turning the label on mid-edit adds a row for its text, so point at it.
        note = f"✅ {opt['key'].replace('_', ' ')} → `{_box_fmt(val)}`"
        if opt["key"] == "Label" and val:
            note += " — set its text with the new row below."
        return self._edit_table(note)

    # ── confirm + build ───────────────────────────────────────────────────
    def _summary(self) -> str:
        p = self.params
        changed = [f"{o['label']} = {_box_fmt(p[o['key']])}" for o in BOX_ADVANCED
                   if p[o["key"]] != o["default"]]
        extra = ("\n**Advanced:** " + "; ".join(changed)) if changed else ""
        label = f"\n**Label:** {p['Label_Text']}" if p["Label"] else ""
        clasps = ("auto — engine picks 1 or 2"
                  if p["Latch_Count"] == BOX_LATCH_AUTO else p["Latch_Count"])
        return (
            f"📦 **{self.name}** — ready to build\n\n"
            f"**Interior:** {p['Width']} × {p['Length']} mm, "
            f"base {p['Bottom_Height']} mm + lid {p['Top_Height']} mm\n"
            f"**Clasps:** {clasps} ({p['Latch_Type']})\n"
            f"**Handle:** {_box_fmt(p['Handle'])}"
            + label + extra + "\n"
            f"**Output:** `{self.boxes_root() / self.name}`\n\n"
            "Reply **go** to build, **edit** to change an answer, "
            "**advance** for more options, or **cancel**."
        )

    async def _execute(self):
        name    = self.name
        out_dir = self.boxes_root() / name
        params  = dict(self.params)
        if not params["Label"]:
            params.pop("Label_Text", None)      # engine ignores it; keeps CLI tidy
        if params["Latch_Count"] == BOX_LATCH_AUTO:
            params["Latch_Count"] = 0           # engine's spelling of "auto"
        self.reset()

        if not BOX_CLI.exists():
            return (f"❌ box-builder isn't installed — expected `{BOX_CLI}`.\n"
                    "Re-extract `dist/box-builder.tar.gz` into the claude-pm repo.")

        self.log.info(f"Box build starting: {name} {params}")
        out_dir.mkdir(parents=True, exist_ok=True)
        res = await asyncio.to_thread(self._run_cli, params, out_dir)
        if isinstance(res, str):
            return res
        return await asyncio.to_thread(self._finish, name, out_dir, params, res)

    def _run_cli(self, params: dict, out_dir: Path):
        cmd = [sys.executable, str(BOX_CLI), "--json", "--out", str(out_dir)] \
            + _box_cli_args(params)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=BOX_TIMEOUT, cwd=str(BOX_ROOT))
        except subprocess.TimeoutExpired:
            return (f"❌ Box generation timed out after {BOX_TIMEOUT // 60} min. "
                    "Try a smaller box or the `clip` clasp.")
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            tail = ((r.stderr or r.stdout or "").strip().splitlines()
                    or ["(no output)"])[-6:]
            self.log.error(f"Box CLI gave no JSON (rc={r.returncode}): {tail}")
            return "❌ Box generation failed:\n```\n" + "\n".join(tail) + "\n```"

    def _finish(self, name: str, out_dir: Path, params: dict, res: dict):
        status = res.get("status")
        if status == "invalid":
            errs = "\n".join(f"  • {e}" for e in res.get("errors", []))
            return (f"❌ **{name}** can't be built with those options:\n{errs}\n\n"
                    "Nothing was rendered. Run `/box` again with different settings.")
        if status == "render_failed":
            errs = "\n".join(f"  • {e}" for e in res.get("errors", [])[:5])
            return f"❌ **{name}** failed to render:\n```\n{errs}\n```"

        files = res.get("files", {})
        shots, warns = box_render_shots(files, out_dir, self.log)
        self.log.info(f"Box build finished: {name} status={status} shots={len(shots)}")

        # The engine reports what "auto" resolved to, so this is the real count.
        n_latch = int(res.get("latch_count") or params.get("Latch_Count") or 0)
        screws = 1 * 2 + 2 * n_latch                # 1/hinge (x2) + 2/latch
        lines = [
            (f"✅ **{name}** — built and verified" if status == "ok"
             else f"⚠️ **{name}** — built, but **printability tests FAILED**"),
            "",
            f"**Interior:** {params['Width']} × {params['Length']} mm, "
            f"base {params['Bottom_Height']} mm + lid {params['Top_Height']} mm",
            f"**Parts:** " + ", ".join(f"1× {p}" if p != "latch"
                                       else f"{n_latch}× latch" for p in files),
            f"**Hardware:** ~{screws} × M3×40 screws",
            f"**Print:** 0.2 mm layers, no supports (Prusa Core One, 0.4 mm nozzle)",
            f"**Render:** {res.get('render_seconds', '?')}s",
            "",
            f"📁 `{out_dir}`",
        ]
        for w in res.get("warnings", []) + warns:
            lines.append(f"⚠️ {w}")
        if status != "ok":
            tail = (res.get("test_output") or "").strip().splitlines()[-8:]
            if tail:
                lines += ["", "```", *tail, "```"]
            lines.append("\n**Don't print this** until the failure is understood.")
        return "\n".join(lines), shots


# ── Top-level command handler ──────────────────────────────────────────────────

class CommandHandler:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg      = cfg
        self.log      = logger
        self._wizards = {}   # user_id -> ProjectWizard
        self._new_forms = {} # user_id -> ProjectWizard behind a /new card

    def _wizard(self, user_id: int):
        """The user's active wizard — a ProjectWizard or a BoxWizard. One slot
        per user: starting one replaces whatever was there."""
        if user_id not in self._wizards:
            self._wizards[user_id] = ProjectWizard(self.cfg, self.log)
        return self._wizards[user_id]

    # ── Wizard plumbing (driven by conversational messages, not slash cmds) ──
    def wizard_active(self, user_id: int) -> bool:
        return self._wizard(user_id).is_active()

    def start_wizard(self, user_id: int) -> str:
        self._wizards[user_id] = ProjectWizard(self.cfg, self.log)
        return self._wizards[user_id].start()

    # ── /new card (see "/new as a tap-through card") ────────────────────────
    def new_form_start(self, user_id: int, name: str, type_key: str,
                       description: str = "") -> tuple:
        """(card text, wizard) for a fresh /new card, or (error, None)."""
        wiz = ProjectWizard(self.cfg, self.log)
        clean, err = wiz.check_name(name)
        if err:
            return f"❌ {err}", None
        key = type_key if type_key in STACKS else "0"
        wiz.data.update(name=clean, description=(description or "").strip(),
                        np_key=key, stack_key=key)
        self.new_form_set_type(wiz, key)
        self._new_forms[user_id] = wiz
        self.log.info(f"/new card: {clean} ({STACKS[key][0]})")
        return new_form_text(wiz), wiz

    def new_form(self, user_id: int):
        return self._new_forms.get(user_id)

    def new_form_set_type(self, wiz, key: str):
        wiz.data["np_key"] = wiz.data["stack_key"] = key if key in STACKS else "0"
        picks = NEW_PICKS.get(wiz._lang())
        wiz.data["np_pick"] = picks[1][0][1] if picks else ""

    def new_form_apply(self, wiz, values: dict) -> str:
        """Take the Details pop-up. Returns a problem to show, or ""."""
        problem = ""
        if "name" in values and values["name"].strip() != wiz.data.get("name"):
            clean, err = wiz.check_name(values["name"])
            if err:
                problem = f"Name kept as `{wiz.data['name']}`: {err}"
            else:
                wiz.data["name"] = clean
        for field, key in (("description", "description"), ("note", "np_note"),
                           ("goals", "goals"), ("rules", "rules")):
            if field in values:
                wiz.data[key] = values[field].strip()
        return problem

    async def new_form_create(self, user_id: int) -> str:
        wiz = self._new_forms.pop(user_id, None)
        if wiz is None:
            return "This card expired (the bot restarted). Run `/new` again."
        new_form_compose(wiz)
        _, err = wiz.check_name(wiz.data["name"])     # taken since the card opened?
        if err:
            self._new_forms[user_id] = wiz
            return f"❌ {err} Tap 📝 Details to rename it."
        return await wiz._execute()

    def new_form_cancel(self, user_id: int) -> str:
        wiz = self._new_forms.pop(user_id, None)
        return (f"✖️ Cancelled; `{wiz.data['name']}` was not created."
                if wiz else "Nothing to cancel.")

    def start_box_wizard(self, user_id: int) -> str:
        self._wizards[user_id] = BoxWizard(self.cfg, self.log)
        return self._wizards[user_id].start()

    async def wizard_input(self, user_id: int, text: str):
        wiz = self._wizard(user_id)
        self.log.info(f"Wizard step [{wiz.state}] input: {text!r}")
        return await wiz.step(text)

    def cancel_wizard(self, user_id: int) -> str:
        wiz = self._wizard(user_id)
        if not wiz.is_active():
            return "No wizard in progress. Use `/new` or `/box` to start one."
        return wiz.cancel()

    def project_names(self) -> list:
        root = Path(self.cfg["projects_root"])
        if not root.exists():
            return []
        return sorted(d.name for d in root.iterdir()
                      if d.is_dir() and not d.name.startswith(".")
                      and d.name != BOXES_DIRNAME)

    def _help(self) -> str:
        return (
            "**Claude Project Manager** — slash commands\n"
            "```\n"
            "── Projects ────────────────────────────────\n"
            "/new <name> <type> Create a project: check the card, tap Create\n"
            "/open <n>         Spawn a Claude Code session\n"
            "/revive <n>       Restart a session (same conversation) + recap\n"
            "/refresh <n>      Brand-new conversation in the same project\n"
            "/debug <a>        start/stop a session on the bot's own code\n"
            "/list             List all projects and status\n"
            "/log <n> [k]      Show newest LOG.md journal entries\n"
            "/stop <n>         Kill a Claude Code session\n"
            "/url <n>          Get the session URL\n"
            "/delete <n> <n>   Soft-delete a project (to .trash/)\n"
            "── Jobs (launch.sh) ────────────────────────\n"
            "/run <n>          Run a project's launch.sh\n"
            "/halt <n>         Stop a running job\n"
            "/jobs             List all running jobs\n"
            "/output <n> [k]   Show last k lines of job output\n"
            "── Visual ──────────────────────────────────\n"
            "/render <n> [f]   Render OpenSCAD model / KiCad board → image\n"
            "/screenshot <n>   Game Boy ROM → emulator frame\n"
            "── Making ──────────────────────────────────\n"
            "/box              Design a printable rugged box (wizard)\n"
            "── System ──────────────────────────────────\n"
            "/status           Daemon health check\n"
            "/health           Full diagnostic: net, auth, CLI version, disk\n"
            "/logs [n] [lvl]   Bot logs (lvl: all/warn/error)\n"
            "/usage            5-hour + weekly usage %\n"
            "/mem <a> [p] [sz] Memory: status / grant / deny / revert / ceiling\n"
            "/cancel           Cancel the active wizard\n"
            "/help             This message\n"
            "── Recovery ────────────────────────────────\n"
            "/relogin          Start a fresh Claude login (fixes bad OAuth)\n"
            "/logincode <c>    Finish /relogin with the pasted code\n"
            "/selfupdate       Update the Claude Code CLI\n"
            "/sudo <a> [p] [s] Approve/revoke/list root scripts per project\n"
            "/reboot <c>       Reboot the laptop (confirm:REBOOT/CANCEL)\n"
            "```\n"
            "Once a session is open, drive it from the **Claude app**.\n"
            "🔔 Auto-alerts: usage (85% 5h / 90% weekly), job crashes, a job's own "
            "`/health` endpoint going unresponsive, session deaths, memory "
            "pressure, idle sessions (12h).\n\n"
            "**🔧 Recovery & system commands, explained:**\n"
            "• `/health` — a full diagnostic of *this bot and its host*, not a "
            "project: is the bot's systemd service up, can it reach Discord/"
            "Anthropic/claude.com, is your Claude login still valid, is the "
            "installed Claude CLI up to date, how much disk is free, how many "
            "sessions/jobs are running, and how many errors/warnings turned up "
            "in the recent log. Run this first whenever something seems broken — "
            "it's what would have caught the 2026-09-26 OAuth outage early.\n"
            "• `/reboot confirm:REBOOT` — reboots the **whole laptop**, not a "
            "session. It waits 60 seconds before actually rebooting (long enough "
            "for `/reboot confirm:CANCEL` to abort if it was a mistake), then the "
            "bot comes back on its own after boot — no login or physical access "
            "needed. Use this if the box seems wedged and nothing else responds.\n"
            "• `/mem` — memory caps, and lifting them. Every session runs under a "
            "soft cap (throttling starts there) and a hard cap (it gets killed there), "
            "so one runaway can't take the laptop down with every other session. When a "
            "job genuinely needs more, the session files the request itself "
            "(`claude-memcheck --request 12G --reason ...`) and the bot posts it here "
            "with the command to answer it: `/mem action:grant project:<n> size:12G` "
            "raises that session's caps **live** — no restart, nothing lost — and the "
            "grant survives a `/revive`. `/mem action:deny` keeps it where it is, "
            "`/mem action:revert` takes a grant back, `/mem action:status` shows every "
            "session's usage against its caps, and `/mem action:ceiling size:<n>` "
            "changes the most any single grant may hand out (13G by default, on a "
            "laptop).\n"
            "• `/selfupdate` — installs the latest Claude Code CLI release from "
            "npm on the laptop. It only affects *new* sessions going forward — "
            "anything already open keeps running its old CLI version until you "
            "`/revive` it (or `/stop` + `/open` it again).\n"
            "• `/relogin` then `/logincode <code>` — the fix when Claude auth is "
            "actually broken (`/health` shows a token error, or sessions fail "
            "with login/auth errors). `/relogin` opens a fresh Claude login flow "
            "on the laptop and sends you back a URL — open that on **any device** "
            "(your phone is fine), log in, and it shows you a short code. Paste "
            "that code with `/logincode <code>` to finish the login from Discord, "
            "no SSH or hands-on-laptop access required. This is what would have "
            "let you fix the stuck OAuth token remotely instead of waiting for it "
            "to self-heal.\n"
            "All four exist so a broken bot, a dead login, or a wedged laptop can "
            "be fixed entirely from your phone."
        )

    def _session_meta(self, sname: str, pname: str, uptimes: dict) -> str:
        """' — up 2h05m · ❗ idle 22h' for a live session.

        Idle is the number that actually tells you whether to act, but printing
        it on every row would double the width of a phone-sized list for no
        gain — a session used minutes ago is obviously not stale. So idle is
        shown only once it clears LIST_IDLE_MIN_SEC, and only then gets a ❗
        when it crosses the same threshold the idle watchdog alerts on, so the
        list agrees with the alerts rather than telling a second story.
        """
        up_sec = uptimes.get(sname, 0)
        parts = [f"up {_fmt_uptime(up_sec)}"]
        idle = session_idle_sec(self.cfg, pname)
        if idle is not None:
            # The transcript can predate the session — /revive starts a fresh
            # session on a project whose last turn is hours old — which would
            # render the nonsense "up 2h · idle 3h". A session can't have been
            # idle longer than it has existed: if it has written nothing since
            # it started, it has been idle for exactly its whole life.
            idle = min(idle, up_sec)
        if idle is not None and idle >= LIST_IDLE_MIN_SEC:
            hours = float(self.cfg.get("session_idle_alert_hours", 12) or 0)
            flag = "❗ " if hours > 0 and idle >= hours * 3600 else ""
            parts.append(f"{flag}idle {_fmt_uptime(int(idle))}")
        return " — " + " · ".join(parts)

    def _list(self) -> str:
        root = Path(self.cfg["projects_root"])
        if not root.exists():
            return f"Projects root `{root}` does not exist yet."
        projects = self.project_names()
        if not projects:
            return "No projects yet. Use `/new` to create one."
        active  = set(active_sessions(self.cfg))
        jobs    = set(active_jobs())
        uptimes = session_uptimes()
        lines   = ["**Projects:**"]
        for p in projects:
            sname = session_name(self.cfg, p)
            live = sname in active
            code_dot = "🟢" if live else "⚫"
            has_launch = (root / p / "launch.sh").exists()
            launch_tag = " 🚀" if has_launch else ""
            meta = self._session_meta(sname, p, uptimes) if live else ""
            lines.append(f"  {code_dot}  `{p}`{launch_tag}{meta}")
            jname = job_session_name(p)
            if jname in jobs:
                # Show what the running script is actually doing, not just
                # that it exists: uptime, port, and a live /health probe.
                up = _fmt_uptime(uptimes.get(jname, 0))
                port = launch_port(root / p / "launch.sh")
                if port:
                    h = "✅ /health" if health_ok(port, timeout=1.5) else "❌ /health"
                    detail = f"launch.sh up {up} — :{port} {h}"
                else:
                    detail = f"launch.sh up {up}"
                lines.append(f"      🟡 {detail}")
        # Sessions with no project folder: the bot-dev sessions each bot opens
        # on its own source (claude-pm's /debug, BoxBot's /debug). They're real
        # sessions this bot can /url, /stop and /output, so list them rather
        # than leaving them invisible.
        known = {session_name(self.cfg, p) for p in projects}
        pfx = self.cfg["tmux_prefix"] + "-"
        external = sorted(s for s in active if s not in known)
        if external:
            lines.append("\n**Bot sessions** *(source dirs, not projects)*:")
            for s in external:
                name = s[len(pfx):]
                lines.append(f"  🔧  `{name}`{self._session_meta(s, name, uptimes)}")

        legend = ("\n🟢 = Claude Code session  🟡 = running job  "
                  "🔧 = bot source  🚀 = has launch.sh")
        if any("❗" in l for l in lines):
            legend += "  ❗ = stale, `/revive` or `/stop`"
        lines.append(legend)
        return "\n".join(lines)

    def _status(self) -> str:
        root     = Path(self.cfg["projects_root"])
        projects = [root / n for n in self.project_names()]
        active   = active_sessions(self.cfg)
        jobs     = active_jobs()
        return (
            f"✅ Daemon running\n"
            f"Projects root: `{self.cfg['projects_root']}`\n"
            f"Projects: {len(projects)} total\n"
            f"Claude Code sessions: {len(active)} active\n"
            f"Jobs running: {len(jobs)}"
        )

    async def _open(self, name: str) -> str:
        if not name:
            return "Usage: `/open <project-name>`"
        if err := bad_project_name(name):
            return err
        root = Path(self.cfg["projects_root"])
        if not (root / name).exists():
            opts = "\n".join(f"  • `{p}`" for p in self.project_names()) or "  (none)"
            return f"No project named `{name}`.\nAvailable:\n{opts}"
        trust_workspace(str(root / name), self.log)
        ok, msg = await spawn_session(self.cfg, name)
        if ok:
            self.log.info(f"Opened session for {name}")
        else:
            self.log.warning(f"Open failed for {name}: {msg.splitlines()[0] if msg else '(no msg)'}")
        return msg

    async def _revive(self, name: str) -> str:
        if not name:
            return "Usage: `/revive <project-name>`"
        if err := bad_project_name(name):
            return err
        if session_dir(self.cfg, name) is None:
            opts = "\n".join(f"  • `{p}`" for p in self.project_names()) or "  (none)"
            return f"No project named `{name}`.\nAvailable:\n{opts}"
        self.log.info(f"Reviving session for {name}...")
        ok, msg = await revive_session(self.cfg, name, self.log)
        if ok:
            self.log.info(f"Revived session for {name}")
        else:
            self.log.warning(f"Revive failed for {name}: {msg.splitlines()[0] if msg else '(no msg)'}")
        return msg

    async def _refresh(self, name: str) -> str:
        if not name:
            return "Usage: `/refresh <project-name>`"
        if err := bad_project_name(name):
            return err
        if session_dir(self.cfg, name) is None:
            opts = "\n".join(f"  • `{p}`" for p in self.project_names()) or "  (none)"
            return f"No project named `{name}`.\nAvailable:\n{opts}"
        self.log.info(f"Refreshing session for {name} (new conversation)...")
        ok, msg = await refresh_session(self.cfg, name, self.log)
        if not ok:
            self.log.warning(f"Refresh failed for {name}: {msg.splitlines()[0] if msg else '(no msg)'}")
        return msg

    async def _debug(self, action: str) -> str:
        """Start/stop a Claude Code session on the bot's own source directory.

        Saves the SSH round-trip when editing claude-pm itself from the phone.
        The session lives OUTSIDE projects_root (in the bot's repo dir) but is
        named like a regular project ("claude-pm"), so /url also works on it.
        Restarting the bot from inside that session is safe — tmux keeps the
        session alive across the systemd restart.
        """
        sname = session_name(self.cfg, "claude-pm")
        if action == "stop":
            if not session_exists(sname):
                return "No bot-dev session running. `/debug start` to open one."
            ok = kill_session(sname)
            self.log.info(f"Debug session stop: {'OK' if ok else 'FAILED'}")
            return ("🛠️ Bot-dev session stopped." if ok
                    else "❌ Could not stop the bot-dev session.")

        bot_dir = Path(__file__).resolve().parent
        trust_workspace(str(bot_dir), self.log)
        ok, msg = await spawn_session(self.cfg, "claude-pm", path=bot_dir)
        if ok:
            msg += (
                "\n🛠️ *Bot-dev session: read `LOG.md` first, and remember to "
                "`systemctl --user restart claude-pm` after changes. "
                "`/debug stop` when done.*"
            )
            self.log.info("Opened bot-dev session (claude-pm)")
        else:
            self.log.warning(f"Bot-dev session failed: {msg.splitlines()[0] if msg else '(no msg)'}")
        return msg

    def _url(self, name: str) -> str:
        if not name:
            return "Usage: `/url <project-name>`"
        if err := bad_project_name(name):
            return err
        sname = session_name(self.cfg, name)
        if not session_exists(sname):
            return f"No active session for `{name}`. Use `/open {name}` to start one."
        url = _extract_env_url(capture_pane(sname))
        if url:
            return f"🔗 **{name}** session URL:\n{url}"
        return f"Session `{name}` is running but no URL found yet. It may still be connecting."

    def _stop(self, name: str) -> str:
        if not name:
            return "Usage: `/stop <project-name>`"
        if err := bad_project_name(name):
            return err
        sname = session_name(self.cfg, name)
        if not session_exists(sname):
            return f"No active session for `{name}`."
        ok = kill_session(sname)
        self.log.info(f"Stop session {sname}: {'OK' if ok else 'FAILED'}")
        # The pane is gone, but daemonized children (Gradle, emulator) aren't.
        reaped = reap_session_scope(sname, self.log) if ok else ""
        note = "\n🧹 Also cleaned up leftover processes." if reaped else ""
        return ("✅ Stopped." + note) if ok else "❌ Could not stop session."

    def _delete(self, name: str, confirm: str) -> str:
        """Soft-delete a project: kill its sessions, move it to `.trash/`.

        Requires `confirm` to exactly match the project name (retype-the-slug),
        mirroring a destructive-action guard. Nothing is permanently removed —
        the folder is moved to `<projects_root>/.trash/<name>-<timestamp>/`.
        """
        if not name:
            return "Usage: `/delete <project-name> <confirm>` (confirm = retype the name)"
        if err := bad_project_name(name):
            return err
        root = Path(self.cfg["projects_root"])
        proj = root / name
        if not proj.exists() or not proj.is_dir():
            opts = "\n".join(f"  • `{p}`" for p in self.project_names()) or "  (none)"
            return f"No project named `{name}`.\nAvailable:\n{opts}"
        if confirm != name:
            return (
                f"⚠️ Confirmation didn't match. To delete **{name}**, run:\n"
                f"`/delete project:{name} confirm:{name}`\n"
                "(The project is moved to `.trash/`, not erased — but the name must match.)"
            )

        # Kill any running Claude session and job for this project first.
        killed = []
        sname = session_name(self.cfg, name)
        if session_exists(sname) and kill_session(sname):
            killed.append("session")
        # Reap before the move: leftover daemons would otherwise keep writing
        # into the project dir after it's been moved to .trash.
        reap_session_scope(sname, self.log)
        jname = job_session_name(name)
        if session_exists(jname) and kill_session(jname):
            killed.append("job")

        trash_dir = root / ".trash"
        try:
            trash_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = trash_dir / f"{name}-{stamp}"
            shutil.move(str(proj), str(dest))
        except OSError as e:
            self.log.error(f"Delete failed for {name}: {e}")
            return f"❌ Could not delete `{name}`: {e}"

        self.log.info(f"Soft-deleted {name} → {dest} (killed: {killed or 'none'})")
        killed_note = f" Stopped its {' and '.join(killed)} first." if killed else ""
        return (
            f"🗑️ **{name}** deleted.{killed_note}\n"
            f"Moved to `{dest}`.\n"
            "It's not gone for good — restore it by moving that folder back, "
            "or clear `.trash/` when you're sure."
        )

    def _run(self, name: str) -> str:
        if not name:
            return "Usage: `/run <project-name>`"
        if err := bad_project_name(name):
            return err
        root = Path(self.cfg["projects_root"])
        proj = root / name
        if not proj.exists():
            return f"No project named `{name}`."
        launch = proj / "launch.sh"
        if not launch.exists():
            return f"No `launch.sh` found in `{name}`. Create the project with a launch type, or add one manually."
        jname = job_session_name(name)
        if session_exists(jname):
            return f"Job `{name}` is already running. Use `halt {name}` first."
        # Same capped scope + shared slice as sessions (a runaway launch.sh
        # used to have no cap at all). The orphan reaper also covers it now:
        # once the job's tmux session ends, anything it left behind is killed.
        scope = _mem_scope_prefix(self.cfg, jname)
        r = subprocess.run(
            ["tmux", "new-session", "-d", "-s", jname, "-c", str(proj)]
            + scope + ["bash", "-c", f"ulimit -c 0; {SESSION_PATH_EXPORT}bash launch.sh"],
            capture_output=True, text=True, timeout=10
        )
        ensure_tmux_server_scope(self.log)
        if r.returncode != 0:
            self.log.error(f"Failed to start job {jname}: {r.stderr.strip()}")
            return f"❌ Failed to start job: {r.stderr.strip()}"
        self.log.info(f"Job started: {jname}")
        return (
            f"✅ Job **{name}** started!\n\n"
            f"Use `output {name}` to see logs\n"
            f"Use `halt {name}` to stop it\n"
            f"Use `jobs` to see all running jobs"
        )

    def _halt(self, name: str) -> str:
        if not name:
            return "Usage: `/halt <project-name>`"
        if err := bad_project_name(name):
            return err
        jname = job_session_name(name)
        if not session_exists(jname):
            return f"No running job for `{name}`."
        ok = kill_session(jname)
        self.log.info(f"Halt job {jname}: {'OK' if ok else 'FAILED'}")
        if ok:
            reap_session_scope(jname, self.log)
        return f"✅ Job **{name}** stopped." if ok else "❌ Could not stop job."

    def _jobs(self) -> str:
        jobs = active_jobs()
        if not jobs:
            return "No jobs running. Use `run <project-name>` to start one."
        root = Path(self.cfg["projects_root"])
        lines = ["**Running jobs:**"]
        for j in jobs:
            pname = j[len(JOB_PREFIX) + 1:]  # strip "job-" prefix
            launch_log = root / pname / "launch.log"
            log_info = ""
            if launch_log.exists():
                last = tail_lines(launch_log, 1)
                if last:
                    log_info = f"  └ `{last[-1][:80]}`"
            lines.append(f"  🟡  `{pname}`")
            if log_info:
                lines.append(log_info)
        return "\n".join(lines)

    def _output(self, name: str, lines: int = 30) -> str:
        if not name:
            return "Usage: `/output <project-name> [lines]`"
        if err := bad_project_name(name):
            return err
        n = max(1, min(lines, 200))

        jname = job_session_name(name)
        root = Path(self.cfg["projects_root"])
        launch_log = root / name / "launch.log"

        # Try launch.log first (persistent), fall back to tmux pane
        if launch_log.exists():
            tail = tail_lines(launch_log, n)
            if tail:
                src = "launch.log"
                output = "\n".join(tail)
            else:
                output = "(log file is empty)"
                src = "launch.log"
        elif session_exists(jname):
            cap = subprocess.run(
                ["tmux", "capture-pane", "-t", _txp(jname), "-p"],
                capture_output=True, text=True, timeout=10
            )
            lines = [l for l in cap.stdout.splitlines() if l.strip()]
            tail = lines[-n:]
            output = "\n".join(tail) if tail else "(no output yet)"
            src = "tmux pane"
        else:
            return f"No running job or log file for `{name}`."

        running = "🟡 running" if session_exists(jname) else "⚫ stopped"
        return f"**{name}** ({running}) — last {len(tail)} lines from {src}:\n```\n{output}\n```"

    def _render(self, name: str, file: str = ""):
        """Render an OpenSCAD model or KiCad board to PNG. Returns
        (text, png_path) on success so the caller can upload the image, or a
        plain error string. Picks the most recently edited renderable file
        (.scad or .kicad_pcb) unless `file` narrows it."""
        if not name:
            return "Usage: `/render <project> [file]`"
        if err := bad_project_name(name):
            return err
        proj = Path(self.cfg["projects_root"]) / name
        if not proj.exists():
            return f"No project named `{name}`."

        scads = [p for p in proj.rglob("*.scad")
                 if ".git" not in p.parts and p.name != "lib.scad"]
        pcbs  = [p for p in proj.rglob("*.kicad_pcb")
                 if ".git" not in p.parts and "-backups" not in str(p)]
        renderables = scads + pcbs
        if not renderables:
            return f"No `.scad` or `.kicad_pcb` files found in `{name}`."
        if file:
            matches = [p for p in renderables if file.lower() in p.name.lower()]
            if not matches:
                opts = "\n".join(f"  • `{p.relative_to(proj)}`"
                                 for p in renderables[:15])
                return f"Nothing matching `{file}`. Found:\n{opts}"
            target = matches[0]
        else:
            target = max(renderables, key=lambda p: p.stat().st_mtime)

        if target.suffix == ".kicad_pcb":
            return self._render_pcb(name, proj, target)
        if not shutil.which("openscad"):
            return "❌ `openscad` is not installed on the host."

        # uuid, not pid: the bot's pid is constant, so concurrent renders of
        # the same project would collide (and reply() deletes after upload).
        out_png = Path(tempfile.gettempdir()) / f"render-{name}-{uuid.uuid4().hex[:8]}.png"
        cmd = ["openscad", "--render", "--autocenter", "--viewall",
               "--imgsize", "1024,1024", "-o", str(out_png), str(target)]
        # PNG export needs a GL context. Prefer Xvfb (works locked/logged-out);
        # fall back to the desktop display if Xvfb isn't installed.
        if shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a"] + cmd
        env = {**os.environ}
        if not shutil.which("xvfb-run") and not env.get("DISPLAY"):
            env["DISPLAY"] = ":0"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env=env)
        if r.returncode != 0 or not out_png.exists():
            err = (r.stderr or "").strip().splitlines()
            tail = "\n".join(err[-6:]) if err else "(no error output)"
            return f"❌ Render failed for `{target.name}`:\n```\n{tail}\n```"

        # Geometry summary from OpenSCAD's stderr (echos + CGAL stats land there).
        info_lines = [l for l in (r.stderr or "").splitlines()
                      if re.search(r"(Vertices|Facets|Volume|ECHO|WARNING)", l)][-5:]
        info = ("\n```\n" + "\n".join(info_lines) + "\n```") if info_lines else ""
        self.log.info(f"Rendered {target.relative_to(proj)} for {name}")
        return (f"🧊 **{name}** — `{target.relative_to(proj)}`{info}", out_png)

    def _render_pcb(self, name: str, proj: Path, target: Path):
        """Raytraced top view of a KiCad board via `kicad-cli pcb render`."""
        if not shutil.which("kicad-cli"):
            return ("❌ `kicad-cli` is not installed — run `install-kicad.sh` "
                    "(in the claude-pm repo) to set up KiCad.")
        out_png = Path(tempfile.gettempdir()) / f"render-{name}-{uuid.uuid4().hex[:8]}.png"
        cmd = ["kicad-cli", "pcb", "render", "--width", "1024", "--height", "1024",
               "-o", str(out_png), str(target)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not out_png.exists():
            err = ((r.stderr or r.stdout or "").strip().splitlines() or ["(no output)"])[-6:]
            return f"❌ Board render failed for `{target.name}`:\n```\n" + "\n".join(err) + "\n```"
        self.log.info(f"Rendered board {target.relative_to(proj)} for {name}")
        return (f"🟩 **{name}** — `{target.relative_to(proj)}` (top, raytraced)", out_png)

    def _screenshot(self, name: str):
        """Screenshot a GBDK ROM via the configured emulator command.
        Returns (text, png_path) or an error/help string."""
        if not name:
            return "Usage: `/screenshot <project>`"
        if err := bad_project_name(name):
            return err
        proj = Path(self.cfg["projects_root"]) / name
        if not proj.exists():
            return f"No project named `{name}`."
        roms = [p for p in proj.rglob("*.gb")] + [p for p in proj.rglob("*.gbc")]
        roms = [p for p in roms if ".git" not in p.parts]
        if not roms:
            return f"No `.gb`/`.gbc` ROM found in `{name}`. Run `make` in a session first."
        rom = max(roms, key=lambda p: p.stat().st_mtime)

        cmd_tpl = self.cfg.get("gb_screenshot_cmd", "")
        if not cmd_tpl:
            return (
                "❌ No Game Boy emulator configured. Set `gb_screenshot_cmd` in "
                "`~/.claude_pm.json` to a command template with `{rom}` and `{out}` "
                "placeholders that runs headless and writes a screenshot, e.g.:\n"
                "```\n"
                "sameboy_tester --time 4 {rom} && mv <produced>.bmp {out}\n"
                "```\n"
                "SameBoy's tester binary or mGBA both work; install one and I can "
                "help wire the exact template."
            )
        out_png = Path(tempfile.gettempdir()) / f"gbshot-{name}-{uuid.uuid4().hex[:8]}.png"
        cmd = cmd_tpl.format(rom=str(rom), out=str(out_png))
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                           timeout=120)
        if r.returncode != 0 or not out_png.exists():
            tail = ((r.stderr or r.stdout or "").strip().splitlines() or ["(no output)"])[-4:]
            return f"❌ Screenshot failed:\n```\n" + "\n".join(tail) + "\n```"
        self.log.info(f"Screenshot of {rom.name} for {name}")
        return (f"🎮 **{name}** — `{rom.relative_to(proj)}`", out_png)

    def _projlog(self, name: str, entries: int = 3) -> str:
        """Show the newest entries from a project's LOG.md change journal."""
        if not name:
            return "Usage: `/log <project> [entries]`"
        if err := bad_project_name(name):
            return err
        proj = Path(self.cfg["projects_root"]) / name
        if not proj.exists():
            return f"No project named `{name}`."
        log_md = proj / "LOG.md"
        if not log_md.exists():
            return f"`{name}` has no LOG.md yet."
        n = max(1, min(entries, 10))
        text = log_md.read_text()[:100_000]
        # Entries are '## ' headers, newest at top; keep any preamble out.
        parts = re.split(r"(?m)^## ", text)
        if len(parts) < 2:
            return f"**{name} — LOG.md** (no entries yet):\n```\n{text[:800]}\n```"
        shown = ["## " + p.strip() for p in parts[1:n + 1]]
        more = len(parts) - 1 - len(shown)
        footer = f"\n*…{more} older entries — open LOG.md in a session for the rest*" if more > 0 else ""
        return f"📖 **{name} — change journal** (newest {len(shown)}):\n\n" + "\n\n".join(shown) + footer

    def _logs(self, lines: int = 50, level: str = "all") -> str:
        n = max(1, min(lines, 200))
        log_path = Path(self.cfg["log_file"])
        if not log_path.exists():
            return "No log file found."
        # Rotation caps the file at ~5 MB, but tail-read anyway: counts cover
        # the last 512 KB, plenty for a level summary.
        all_lines = tail_lines(log_path, 0, max_bytes=524288)
        if not all_lines:
            return "Log file is empty."

        err_count  = sum(1 for l in all_lines if "[ERROR]" in l)
        warn_count = sum(1 for l in all_lines if "[WARNING]" in l)
        info_count = sum(1 for l in all_lines if "[INFO]" in l)

        lv = level.lower()
        if lv in ("error", "err"):
            filtered = [l for l in all_lines if "[ERROR]" in l]
            label = "ERROR lines"
        elif lv in ("warning", "warn"):
            filtered = [l for l in all_lines if "[WARNING]" in l or "[ERROR]" in l]
            label = "WARN+ lines"
        else:
            filtered = all_lines
            label = "lines"

        tail = filtered[-n:]

        formatted = []
        for line in tail:
            if "[ERROR]" in line:
                formatted.append("❌ " + line)
            elif "[WARNING]" in line:
                formatted.append("⚠️  " + line)
            else:
                formatted.append("   " + line)

        size_kb = log_path.stat().st_size / 1024
        header = (
            f"**Bot Logs** — `{log_path}` ({size_kb:.1f} KB, last {len(all_lines)} lines scanned)\n"
            f"ℹ️ {info_count} INFO  ⚠️ {warn_count} WARN  ❌ {err_count} ERROR\n"
            f"Showing last {len(tail)} {label}:\n"
            "```\n"
        )
        return header + "\n".join(formatted) + "\n```"

    def _health(self) -> str:
        """One-shot diagnostic pass — service, network, auth, CLI version, disk, sessions.

        Built after the 2026-09-26 lockout: the OAuth refresh grant failed for
        ~9h straight with nothing surfacing it short of tailing the raw log.
        This is the "is the bot actually okay" check to run first when
        something seems off.
        """
        lines = ["**🩺 Health Check**"]

        svc = _service_health("claude-pm.service")
        state, sub = svc.get("ActiveState", "?"), svc.get("SubState", "?")
        restarts = svc.get("NRestarts", "?")
        lines.append(f"{'✅' if state == 'active' else '❌'} Bot service: "
                     f"{state}/{sub} (restarts: {restarts})")

        for host in ("discord.com", "api.anthropic.com", "platform.claude.com"):
            ok, detail = _tcp_check(host)
            lines.append(f"{'✅' if ok else '❌'} {host}: {detail}")

        auth = _auth_status()
        if auth.get("loggedIn"):
            lines.append(f"✅ Claude auth: logged in as {auth.get('email', '?')} "
                         f"({auth.get('subscriptionType', '?')})")
        elif auth:
            lines.append("❌ Claude auth: NOT logged in — run `/relogin`")
        else:
            exp = _token_expires_at()
            remaining = (exp - _now_sec()) if exp else None
            if remaining is None:
                lines.append("⚠️ Claude auth: status check failed/timed out")
            elif remaining > 0:
                lines.append(f"⚠️ Claude auth: status check failed, but the token file "
                             f"says valid for {_fmt_idle(remaining)} more")
            else:
                lines.append(f"❌ Claude auth: token expired {_fmt_idle(-remaining)} ago "
                             f"— run `/relogin`")

        live = probe_rate_limits(self.log)
        if live:
            lines.append(f"✅ Usage probe: 5h {live['pct5']:.0f}% "
                         f"(via `{_probe_cache.get('model')}`)")
        else:
            lines.append("❌ Usage probe failing: "
                         f"{_probe_cache.get('last_err') or 'no rate-limit data'} — "
                         "`/usage` and the usage alerts are blind until it works")

        installed, latest = _claude_cli_versions()
        if installed and latest:
            lines.append(f"{'✅' if installed == latest else '⚠️'} Claude CLI: {installed}"
                         + ("" if installed == latest
                            else f" (latest: {latest} — try `/selfupdate`)"))
        elif installed:
            lines.append(f"ℹ️ Claude CLI: {installed} (couldn't reach npm to compare)")
        else:
            lines.append("❌ `claude` CLI not found on PATH")

        active = active_sessions(self.cfg)
        jobs = active_jobs()
        lines.append(f"ℹ️ Sessions: {len(active)} active, {len(jobs)} job(s) running")

        pw = power_state()
        if pw["on_ac"] is not None:
            pct = f" ({pw['pct']}%)" if pw["pct"] is not None else ""
            lines.append(f"✅ Power: plugged in{pct}" if pw["on_ac"]
                         else f"⚠️ Power: ON BATTERY{pct} — plug the cord in")

        du = shutil.disk_usage(Path.home())
        free_gb = du.free / 1e9
        lines.append(f"{'✅' if free_gb > 5 else '⚠️'} Disk free: {free_gb:.1f} GB")

        log_path = Path(self.cfg["log_file"])
        if log_path.exists():
            tail = tail_lines(log_path, 0, max_bytes=131072)
            err = sum(1 for l in tail if "[ERROR]" in l)
            warn = sum(1 for l in tail if "[WARNING]" in l)
            lines.append(f"{'⚠️' if err or warn else '✅'} Recent log: {err} ERROR, "
                         f"{warn} WARNING (last {len(tail)} lines)")

        return "\n".join(lines)

    def _reboot(self, confirm: str) -> str:
        """Reboot the whole laptop, or cancel a pending one.

        `confirm:REBOOT` schedules it 60s out (`shutdown -r +1`) rather than
        firing instantly — long enough for this reply to reach Discord and for
        `confirm:CANCEL` to abort it if it was a mistake. Relies on the box's
        narrow sudoers grant (see SUDOERS_RULES) and that claude-pm.service is
        `enabled` with linger on, so the bot comes back on its own after boot.
        """
        if confirm == "CANCEL":
            r = subprocess.run(SUDO_SHUTDOWN_CANCEL,
                               capture_output=True, text=True, timeout=10)
            ok = r.returncode == 0
            self.log.info(f"Reboot cancel: {'OK' if ok else r.stderr.strip()}")
            return ("🛑 Scheduled reboot cancelled." if ok else
                    f"❌ Could not cancel (no reboot pending?): {r.stderr.strip()}")
        if confirm != "REBOOT":
            return (
                "⚠️ This restarts the whole laptop — every running session and job "
                "dies and won't come back for ~1-2 minutes.\n"
                "To confirm: `/reboot confirm:REBOOT`\n"
                "To cancel a pending reboot: `/reboot confirm:CANCEL`"
            )
        r = subprocess.run(SUDO_SHUTDOWN_REBOOT,
            capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            self.log.error(f"Reboot request failed: {r.stderr.strip()}")
            return f"❌ Reboot request failed: {r.stderr.strip()}"
        self.log.warning("Reboot scheduled via Discord (/reboot) — rebooting in 60s")
        return (
            "🔄 Laptop will reboot in **60 seconds**.\n"
            "The bot service is enabled with linger on, so it comes back on its own "
            "after boot — give it ~1-2 min, then `/health` to confirm.\n"
            "Changed your mind? `/reboot confirm:CANCEL` right now."
        )

    def _selfupdate(self) -> str:
        """Update the Claude Code CLI to the latest npm release.

        Only the CLI binary — sessions already running keep their old process
        until `/revive`d or reopened. Doesn't touch this bot's own service.
        """
        installed_before, latest = _claude_cli_versions()
        if not latest:
            return "❌ Could not reach npm to check the latest version."
        if installed_before == latest:
            return f"✅ Already up to date (`{installed_before}`)."
        self.log.info(f"Self-update: {installed_before} → {latest}")
        if not re.fullmatch(r"[0-9][0-9.]*", latest):
            return f"❌ Unexpected version string from npm: `{latest}`"
        r = subprocess.run(SUDO_NPM_UPDATE + [f"@anthropic-ai/claude-code@{latest}"],
            capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            self.log.error(f"Self-update failed: {r.stderr.strip()[-500:]}")
            return f"❌ Update failed:\n```\n{r.stderr.strip()[-1500:]}\n```"
        installed_after, _ = _claude_cli_versions()
        self.log.info(f"Self-update done: now {installed_after}")
        return (
            f"✅ Updated Claude Code CLI: `{installed_before}` → `{installed_after}`.\n"
            "Existing sessions keep running the old binary — `/revive` (or `/stop` + "
            "`/open`) each project to pick it up."
        )

    # ── /mem: the memory cap is a soft block the owner can lift ────────────────

    def _mem_units(self, project: str) -> list:
        """Live scope units for a project — its session and/or its job."""
        out = []
        for sname in (session_name(self.cfg, project), job_session_name(project)):
            unit = f"{_scope_unit(sname)}.scope"
            if not _scope_gone(unit):
                out.append(unit)
        return out

    def _mem(self, action: str = "status", project: str = "", size: str = "",
             soft: str = "") -> str:
        """/mem status | grant | deny | revert | ceiling.

        The per-session cap exists so one runaway can't take the laptop (and
        every other session) down. It is deliberately *not* final: a session
        that needs more files a request from inside its own scope and keeps
        working under the old cap until the owner answers here. A grant is applied
        to the running scope immediately and remembered, so /revive keeps it.
        """
        cfg = self.cfg
        ceiling = parse_size(cfg.get("mem_grant_ceiling", "13G")) or 0
        grants, reqs = read_mem_grants(), read_mem_requests()

        if action == "status":
            lines = ["🧠 **Memory**"]
            dflt = (cfg.get("session_mem_high"), cfg.get("session_mem_max"))
            lines.append(f"  default per session: {dflt[0]} soft / {dflt[1]} hard · "
                         f"shared ceiling {cfg.get('sessions_total_mem_high')} / "
                         f"{cfg.get('sessions_total_mem_max')} · grants up to "
                         f"{cfg.get('mem_grant_ceiling', '13G')}")
            names = sorted(set(active_sessions(cfg)) | set(active_jobs()))
            units = [(f"{_scope_unit(n)}.scope", _sname_project(cfg, n)) for n in names]
            units.append((SESSIONS_SLICE, "— all sessions + jobs —"))
            for unit, who in units:
                d = unit_mem_detail(unit)
                if not d:
                    continue
                mark = " 🔓" if who in grants else ""
                gib = 1024 ** 3
                lines.append(f"  `{who}`{mark}: {d['used'] / gib:.1f} GB used of "
                             f"{(d['soft'] or 0) / gib:.0f} GB soft"
                             + (f" / {d['max'] / gib:.0f} GB hard" if d.get("max") else "")
                             + (f" · {d['swap'] / gib:.1f} GB swap" if d["swap"] > 64e6 else "")
                             + (f" · stalled {d['psi']:.0f}%" if d["psi"] >= 1 else ""))
            if grants:
                lines.append("**Grants** (survive /revive):")
                for proj, g in sorted(grants.items()):
                    lines.append(f"  `{proj}` → {g.get('high', '?')} soft / {g.get('max', '?')} hard"
                                 + (f" — {g['reason']}" if g.get("reason") else "")
                                 + f"  ·  `/mem action:revert project:{proj}` to undo")
            pending = {k: v for k, v in reqs.items() if v.get("status") == "pending"}
            if pending:
                lines.append("**Waiting on you:**")
                for proj, r in sorted(pending.items()):
                    lines.append(f"  `{proj}` asks for {r.get('ask_max', '?')} — "
                                 f"{r.get('reason', 'no reason given')}\n"
                                 f"    `/mem action:grant project:{proj} size:{r.get('ask_max', '')}`"
                                 f" · `/mem action:deny project:{proj}`")
            else:
                lines.append("No pending requests.")
            return "\n".join(lines)

        if action == "ceiling":
            n = parse_size(size)
            if not n:
                return ("Give the new ceiling, e.g. `/mem action:ceiling size:14G`. "
                        f"It's {cfg.get('mem_grant_ceiling', '13G')} now — the most "
                        "any one grant may hand out.")
            if n > 15 * 1024 ** 3:
                return (f"{fmt_size(n)} is more than the laptop has (your RAM). "
                        "Pick something that leaves room for the desktop and this bot.")
            cfg["mem_grant_ceiling"] = fmt_size(n)
            try:
                save_config(cfg)
            except OSError as e:
                return f"❌ Could not save the new ceiling: {e}"
            self.log.info(f"mem ceiling → {fmt_size(n)}")
            return f"✅ Grant ceiling is now **{fmt_size(n)}**."

        if err := bad_project_name(project):
            return err

        if action == "deny":
            req = resolve_mem_request(project, "denied")
            if not req:
                return f"`{project}` hasn't asked for more memory."
            self.log.info(f"mem request denied for {project}")
            return (f"🚫 Denied `{project}`'s request for {req.get('ask_max', '?')}. "
                    "Tell the session to shrink the job — it stays on its current cap.")

        if action == "revert":
            if project not in grants:
                return f"`{project}` has no grant — it's already on the default cap."
            set_mem_grant(project, "", "", "")
            high, mx, swap = (cfg.get("session_mem_high"), cfg.get("session_mem_max"),
                              cfg.get("session_mem_swap_max"))
            applied = [u for u in self._mem_units(project)
                       if apply_mem_caps_live(u, high, mx, swap)[0]]
            self.log.info(f"mem grant reverted for {project}")
            return (f"↩️ `{project}` is back to the default {high} soft / {mx} hard"
                    + (f" (applied live to {len(applied)} running scope(s))." if applied
                       else " — it takes effect the next time it starts."))

        if action != "grant":
            return "Usage: `/mem action:status|grant|deny|revert|ceiling`."

        req = reqs.get(project, {})
        want = parse_size(size) or parse_size(req.get("ask_max", ""))
        if not want:
            return ("Give the new hard cap, e.g. "
                    f"`/mem action:grant project:{project} size:12G`.")
        if want > ceiling:
            return (f"⛔ {fmt_size(want)} is above the {fmt_size(ceiling)} grant ceiling. "
                    f"Raise it first (`/mem action:ceiling size:{fmt_size(want)}`) if the "
                    "laptop really can spare that, then grant again.")
        # Keep the default head-room between soft and hard, but never hand out
        # a soft cap *below* the default one: granting 9G used to drop the
        # throttle point from 6G to 5G, i.e. make the session worse off.
        dflt_high = parse_size(cfg.get("session_mem_high", "6G")) or 0
        gap = max((parse_size(cfg.get("session_mem_max", "10G")) or 0) - dflt_high,
                  1024 ** 3)
        want_soft = parse_size(soft) or max(dflt_high, want - gap)
        if want_soft >= want:
            return (f"The soft cap ({fmt_size(want_soft)}) has to be below the hard "
                    f"cap ({fmt_size(want)}) — ask for a bigger hard cap, or set "
                    "`soft:` explicitly.")

        # The shared slice bounds every session together, so a grant above it
        # would be silently capped there. Raise it with the grant (never past
        # the ceiling) and say so.
        slice_note = ""
        slice_max = parse_size(cfg.get("sessions_total_mem_max", "")) or 0
        if slice_max and want > slice_max:
            new_slice = min(ceiling, max(want, slice_max))
            cfg["sessions_total_mem_max"] = fmt_size(new_slice)
            slice_high = parse_size(cfg.get("sessions_total_mem_high", "")) or 0
            if slice_high and slice_high >= new_slice:
                cfg["sessions_total_mem_high"] = fmt_size(max(1024 ** 3, new_slice - 1024 ** 3))
            try:
                save_config(cfg)
            except OSError as e:
                self.log.warning(f"Could not persist slice ceiling: {e}")
            _slice_ok["ok"] = False          # force a rewrite + daemon-reload
            ensure_sessions_slice(cfg)
            apply_mem_caps_live(SESSIONS_SLICE, cfg.get("sessions_total_mem_high"),
                                cfg.get("sessions_total_mem_max"),
                                cfg.get("sessions_total_swap_max"))
            slice_note = (f"\nShared ceiling raised to {cfg['sessions_total_mem_max']} "
                          "so the grant isn't capped there.")

        swap = cfg.get("session_mem_swap_max", "")
        set_mem_grant(project, fmt_size(want_soft), fmt_size(want), swap,
                      req.get("reason", ""))
        units = self._mem_units(project)
        live, failed = [], []
        for u in units:
            ok, detail = apply_mem_caps_live(u, fmt_size(want_soft), fmt_size(want), swap)
            (live if ok else failed).append(u if ok else f"{u} ({detail})")
        resolve_mem_request(project, "granted")
        self.log.info(f"mem grant {project}: {fmt_size(want_soft)}/{fmt_size(want)} "
                      f"live={live} failed={failed}")
        where = (f"Applied live to {', '.join(live)} — no restart needed."
                 if live else "No scope is running; it applies when the session starts.")
        return (f"✅ `{project}` may now use **{fmt_size(want_soft)} soft / "
                f"{fmt_size(want)} hard**.{slice_note}\n{where}\n"
                f"The grant survives /revive — `/mem action:revert project:{project}` to undo.")

    def _sudo(self, action: str, project: str = "", script: str = "",
              confirm: str = "") -> str:
        """/sudo list | approve | revoke: which project scripts may run as root.

        Approval is two-step on purpose: the first call shows the script and
        its hash; only `confirm:<first 8 of that hash>` stores it, so what gets
        approved is exactly the version the owner read. The gate (cpm-sudo) pins
        the full hash, so any later edit voids the approval.
        """
        import hashlib
        approvals = _read_json(SUDO_ETC / "approved.json", {})
        if not SUDO_GATE.exists():
            return ("🔐 The sudo gate isn't installed yet, so sessions still have "
                    "unrestricted sudo. On the laptop: "
                    "`sudo bash ~/claude-pm/install-sudo-gate.sh`")
        if action == "list":
            if not any(approvals.values()):
                return ("🔐 No approved root scripts. Sessions other than "
                        "claude-pm can't use sudo until you `/sudo approve` one.")
            lines = ["🔐 **Approved root scripts**"]
            for proj, paths in sorted(approvals.items()):
                for p, sha in sorted(paths.items()):
                    try:
                        cur = hashlib.sha256(Path(p).read_bytes()).hexdigest()
                        st = "✅" if cur == sha else "⚠️ changed since approval (inactive)"
                    except OSError:
                        st = "❌ missing"
                    lines.append(f"  `{proj}` · `{p}` {st}")
            return "\n".join(lines)
        if err := bad_project_name(project):
            return err
        proj = (Path(self.cfg["projects_root"]) / project).resolve()
        if not proj.is_dir():
            return f"No project named `{project}`."
        if not script:
            return "Give the script path, relative to the project (e.g. `tools/setup.sh`)."
        path = (proj / script).resolve()
        if proj not in path.parents or not path.is_file():
            return f"❌ `{script}` isn't a file inside `{project}`."
        rel = path.relative_to(proj)
        if action == "revoke":
            r = subprocess.run(["sudo", "-n", str(SUDO_GATE), "--revoke", project, str(path)],
                               capture_output=True, text=True, timeout=15)
            self.log.info(f"sudo revoke {project}:{rel} rc={r.returncode}")
            return (f"🔒 Revoked `{rel}` for **{project}**." if r.returncode == 0
                    else f"Nothing to revoke: `{rel}` wasn't approved for {project}.")
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if confirm != sha[:8]:
            text = data.decode(errors="replace").splitlines()
            preview = "\n".join(text[:40]) + (f"\n… ({len(text) - 40} more lines)"
                                              if len(text) > 40 else "")
            return (f"🔐 **Review before approving**: `{project}/{rel}` "
                    f"({len(text)} lines, hash `{sha[:8]}`)\n```bash\n{preview}\n```\n"
                    f"Approving lets the **{project}** session run this **as root** "
                    "(`sudo " + str(rel) + "`), including every `sudo` command inside "
                    "it. Editing the file later cancels the approval.\nTo approve exactly "
                    f"this version: `/sudo action:approve project:{project} "
                    f"script:{rel} confirm:{sha[:8]}`")
        r = subprocess.run(["sudo", "-n", str(SUDO_GATE), "--approve", project, str(path)],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return f"❌ Approval failed: {(r.stderr or r.stdout).strip()[:300]}"
        if r.stdout.strip() != sha:   # file changed between preview and approval
            subprocess.run(["sudo", "-n", str(SUDO_GATE), "--revoke", project, str(path)],
                           capture_output=True, timeout=15)
            return "❌ The script changed while you were approving it. Review it again."
        self.log.info(f"sudo approve {project}:{rel} sha={sha[:12]}")
        return (f"✅ **{project}** may now run `sudo {rel}` (hash `{sha[:8]}`). "
                f"`/sudo action:revoke project:{project} script:{rel}` to undo.")

    async def _relogin(self) -> str:
        self.log.info("Relogin: starting login flow")
        ok, msg = await start_relogin(self.log)
        return msg

    async def _logincode(self, code: str) -> str:
        if not code:
            return "Usage: `/logincode <code>` — paste the code from the login page."
        ok, msg = await submit_relogin_code(code, self.log)
        return msg

    @staticmethod
    def _pct_bar(pct: float) -> str:
        """20-char █░ bar for an already-computed percentage (live probe data)."""
        filled = min(20, int(round(pct / 5)))
        return f"`{'█' * filled}{'░' * (20 - filled)}` {pct:.0f}%"

    @staticmethod
    def _fmt_reset(reset_at, now) -> str:
        if reset_at is None:
            return "unknown"
        secs = max(0, int((reset_at - now).total_seconds()))
        local = reset_at.astimezone()
        if secs >= 86400:
            return f"{local.strftime('%a %-I:%M %p')} ({secs // 86400}d {secs % 86400 // 3600}h)"
        return f"in {secs // 3600} hr {secs % 3600 // 60} min ({local.strftime('%-I:%M %p')})"

    def _usage(self) -> str:
        """5-hour and weekly usage, straight from Anthropic's rate-limit headers.

        Deliberately minimal: the Claude app now has full usage tracking, so
        this is just the two numbers that decide whether to keep going. (It
        used to estimate usage from local session logs with calibrated weights,
        /sync baseline offsets and per-model/daily tables; all removed
        2026-10-15.)
        """
        live = probe_rate_limits(self.log)
        if not live:
            return ("❌ Couldn't read usage right now (the live probe failed; "
                    "`/health` shows why). The Claude app → Settings → Usage has "
                    "the same numbers.")
        now = live["now"]
        plan = claude_plan_info()["plan"]
        lines = [f"**Claude usage**{f' — {plan.title()}' if plan else ''}",
                 f"5-hour  {self._pct_bar(live['pct5'])} · resets "
                 f"{self._fmt_reset(live['reset5'], now)}"]
        if live["pctw"] is not None:
            lines.append(f"Weekly  {self._pct_bar(live['pctw'])} · resets "
                         f"{self._fmt_reset(live['resetw'], now)}")
        if live["status"] and live["status"] != "allowed":
            lines.append(f"⚠️ status: **{live['status']}**")
        return "\n".join(lines)


# ── Discord bot ───────────────────────────────────────────────────────────────

async def run_bot(cfg: dict, logger: logging.Logger):
    try:
        import discord
        from discord import app_commands
    except ImportError:
        logger.error("discord.py not installed. Run: pip3 install --user discord.py")
        sys.exit(1)

    token      = cfg.get("discord_token", "")
    channel_id = cfg.get("discord_channel_id", "")

    if not token:
        logger.error("discord_token not set. Run: python3 claude_pm.py --setup")
        sys.exit(1)
    if not channel_id:
        logger.error("discord_channel_id not set. Run: python3 claude_pm.py --setup")
        sys.exit(1)

    try:
        channel_id_int = int(channel_id)
    except ValueError:
        logger.error("discord_channel_id must be a numeric ID.")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client  = discord.Client(intents=intents)
    tree    = app_commands.CommandTree(client)
    handler = CommandHandler(cfg, logger)

    # ── Helpers ────────────────────────────────────────────────────────────
    async def project_autocomplete(interaction, current: str):
        return [
            app_commands.Choice(name=n, value=n)
            for n in handler.project_names()
            if current.lower() in n.lower()
        ][:25]

    async def send_attachments(send, attach):
        """Upload one attachment or a list of them via `send`.

        Temp files are deleted afterwards (that's how /render and /screenshot
        clean up), but files that live alongside their project — a box's
        renders, which the user is being handed the folder for — are kept.
        """
        if not attach:
            return
        paths = [attach] if isinstance(attach, (str, Path)) else list(attach)
        tmp_root = Path(tempfile.gettempdir())
        for p in paths:
            p = Path(p)
            try:
                await send(file=discord.File(str(p)))
            except Exception as e:
                # Upload can fail (file too large, transient Discord error) —
                # tell the user instead of dying silently after the text part.
                logger.warning(f"Attachment upload failed for {p}: {e}")
                await send(f"⚠️ Image upload failed for `{p.name}`: {e}")
            finally:
                if p.is_relative_to(tmp_root):
                    p.unlink(missing_ok=True)

    async def reply(interaction, result):
        """Send a (possibly long) response as deferred followups.

        `result` is a string, or a (text, attachment) tuple from handlers that
        produce images (/render, /screenshot, /box), where `attachment` is one
        path or a list of them.
        """
        text, attach = result if isinstance(result, tuple) else (result, None)
        for chunk in _smart_chunks(text, 1900):
            if chunk.strip():   # Discord rejects empty messages
                await interaction.followup.send(chunk)
        await send_attachments(interaction.followup.send, attach)

    # Who to @-mention in watchdog alerts. Seeded from config; auto-filled from
    # the first command issued in the channel if not set, and persisted.
    owner = {"id": str(cfg.get("owner_user_id") or "") or None}

    def remember_owner(uid):
        if owner["id"] is None and uid:
            owner["id"] = str(uid)
            cfg["owner_user_id"] = str(uid)
            try:
                save_config(cfg)
                logger.info(f"Watchdog owner set to user {uid}")
            except Exception as e:
                logger.warning(f"Could not persist owner_user_id: {e}")

    async def post_channel(text: str):
        ch = client.get_channel(channel_id_int) or await client.fetch_channel(channel_id_int)
        for chunk in _smart_chunks(text, 1900):
            await ch.send(chunk)

    def guard(interaction) -> bool:
        if interaction.channel_id == channel_id_int:
            remember_owner(interaction.user.id)
            return True
        return False

    async def run_cmd(interaction, backend):
        """Defer FIRST, then run the backend, then reply.

        `backend` is a coroutine (async handlers) or a zero-arg callable (sync
        handlers, run via to_thread so file/subprocess work can't block the
        event loop or trip Discord's 3-second interaction deadline — /usage
        parses 80+ MB of session JSONL). Callables must be passed unevaluated
        (`lambda: handler._x(...)`) — evaluating at the call site would run
        them before the defer, which was the original timeout bug.
        """
        if not guard(interaction):
            await interaction.response.send_message(
                "This bot only responds in its configured channel.", ephemeral=True)
            return
        try:
            await interaction.response.defer(thinking=True)
        except Exception as e:
            # Interaction token already expired/acked — nothing we can reply to.
            logger.warning(f"Defer failed (expired interaction?): {e}")
            return
        try:
            if asyncio.iscoroutine(backend):
                result = await backend
            elif callable(backend):
                result = await asyncio.to_thread(backend)
            else:
                result = backend
        except Exception as e:
            logger.exception("Command failed")
            result = f"❌ Command failed: {e}"
        await reply(interaction, result or "(no output)")

    # ── Slash commands ─────────────────────────────────────────────────────
    @tree.command(name="new", description="Create a project: name + type, then check it over and tap Create")
    @app_commands.describe(
        name="project folder name (lowercase, hyphens OK, e.g. my-api)",
        type="what kind of project; picks the scaffold and skills",
        description="optional: what it does (you can also add it on the card)")
    @app_commands.choices(type=[
        app_commands.Choice(name=lbl[:100], value=k)
        for k, (lbl, *_rest) in sorted(STACKS.items(),
                                        key=lambda kv: (kv[0] == "0", kv[0]))])
    async def new_cmd(interaction, name: str, type: str, description: str = ""):
        if not guard(interaction):
            await interaction.response.send_message(
                "This bot only responds in its configured channel.", ephemeral=True)
            return
        text, wiz = handler.new_form_start(interaction.user.id, name, type, description)
        if wiz is None:
            await interaction.response.send_message(text, ephemeral=True)
            return
        await interaction.response.send_message(
            text, view=new_form_view(interaction.user.id, wiz), ephemeral=True)

    @tree.command(name="box", description="Design a 3D-printable rugged box (wizard)")
    async def box_cmd(interaction):
        if not guard(interaction):
            await interaction.response.send_message(
                "This bot only responds in its configured channel.", ephemeral=True)
            return
        msg = handler.start_box_wizard(interaction.user.id)
        await interaction.response.send_message(msg)

    @tree.command(name="cancel", description="Cancel the active project wizard")
    async def cancel_cmd(interaction):
        await run_cmd(interaction, lambda: handler.cancel_wizard(interaction.user.id))

    @tree.command(name="open", description="Spawn a Claude Code session for a project")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def open_cmd(interaction, project: str):
        await run_cmd(interaction, handler._open(project))

    @tree.command(name="revive", description="Restart a stale session (same conversation) + recap")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def revive_cmd(interaction, project: str):
        await run_cmd(interaction, handler._revive(project))

    @tree.command(name="refresh", description="Stop a session and start a brand-new conversation in the same project")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def refresh_cmd(interaction, project: str):
        await run_cmd(interaction, handler._refresh(project))

    @tree.command(name="debug", description="Start/stop a Claude Code session on the bot's own code (claude-pm)")
    @app_commands.describe(action="start or stop the bot-dev session")
    @app_commands.choices(action=[
        app_commands.Choice(name="start", value="start"),
        app_commands.Choice(name="stop", value="stop"),
    ])
    async def debug_cmd(interaction, action: str):
        await run_cmd(interaction, handler._debug(action))

    @tree.command(name="list", description="List all projects and their session status")
    async def list_cmd(interaction):
        await run_cmd(interaction, lambda: handler._list())

    @tree.command(name="stop", description="Kill a project's Claude Code session")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def stop_cmd(interaction, project: str):
        await run_cmd(interaction, lambda: handler._stop(project))

    @tree.command(name="url", description="Get the connection URL for a running session")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def url_cmd(interaction, project: str):
        await run_cmd(interaction, lambda: handler._url(project))

    @tree.command(name="delete", description="Soft-delete a project (moves it to .trash/)")
    @app_commands.describe(project="Project name",
                           confirm="Retype the project name to confirm")
    @app_commands.autocomplete(project=project_autocomplete)
    async def delete_cmd(interaction, project: str, confirm: str):
        await run_cmd(interaction, lambda: handler._delete(project, confirm))

    @tree.command(name="run", description="Run a project's launch.sh as a background job")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def run_job_cmd(interaction, project: str):
        await run_cmd(interaction, lambda: handler._run(project))

    @tree.command(name="halt", description="Stop a running launch.sh job")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def halt_cmd(interaction, project: str):
        await run_cmd(interaction, lambda: handler._halt(project))

    @tree.command(name="jobs", description="List all running launch.sh jobs")
    async def jobs_cmd(interaction):
        await run_cmd(interaction, lambda: handler._jobs())

    @tree.command(name="output", description="Show recent output from a project's job")
    @app_commands.describe(project="Project name", lines="How many lines (default 30)")
    @app_commands.autocomplete(project=project_autocomplete)
    async def output_cmd(interaction, project: str, lines: int = 30):
        await run_cmd(interaction, lambda: handler._output(project, lines))

    @tree.command(name="render", description="Render an OpenSCAD model or KiCad board to an image")
    @app_commands.describe(project="Project name",
                           file="Which .scad/.kicad_pcb file (default: most recently edited)")
    @app_commands.autocomplete(project=project_autocomplete)
    async def render_cmd(interaction, project: str, file: str = ""):
        await run_cmd(interaction, lambda: handler._render(project, file))

    @tree.command(name="screenshot", description="Screenshot a Game Boy ROM in the emulator")
    @app_commands.describe(project="Project name")
    @app_commands.autocomplete(project=project_autocomplete)
    async def screenshot_cmd(interaction, project: str):
        await run_cmd(interaction, lambda: handler._screenshot(project))

    @tree.command(name="log", description="Show a project's LOG.md change journal")
    @app_commands.describe(project="Project name", entries="How many entries (default 3)")
    @app_commands.autocomplete(project=project_autocomplete)
    async def projlog_cmd(interaction, project: str, entries: int = 3):
        await run_cmd(interaction, lambda: handler._projlog(project, entries))

    @tree.command(name="status", description="Daemon health check")
    async def status_cmd(interaction):
        await run_cmd(interaction, lambda: handler._status())

    @tree.command(name="logs", description="Show bot log lines with level summary")
    @app_commands.describe(lines="How many lines (default 50)", level="Filter: all / warn / error")
    async def logs_cmd(interaction, lines: int = 50, level: str = "all"):
        await run_cmd(interaction, lambda: handler._logs(lines, level))

    @tree.command(name="usage", description="Claude usage: 5-hour and weekly %")
    async def usage_cmd(interaction):
        await run_cmd(interaction, lambda: handler._usage())

    @tree.command(name="health", description="Full diagnostic: service, network, auth, CLI version, disk, sessions")
    async def health_cmd(interaction):
        await run_cmd(interaction, lambda: handler._health())

    @tree.command(name="reboot", description="Reboot the laptop (or cancel a pending one)")
    @app_commands.describe(confirm="Type REBOOT to confirm, or CANCEL to abort a pending reboot")
    async def reboot_cmd(interaction, confirm: str = ""):
        await run_cmd(interaction, lambda: handler._reboot(confirm))

    @tree.command(name="selfupdate", description="Update the Claude Code CLI to the latest release")
    async def selfupdate_cmd(interaction):
        await run_cmd(interaction, lambda: handler._selfupdate())

    @tree.command(name="relogin", description="Start a fresh Claude login flow (fixes an expired/broken OAuth token)")
    async def relogin_cmd(interaction):
        await run_cmd(interaction, handler._relogin())

    @tree.command(name="logincode", description="Finish /relogin by submitting the code from the login page")
    @app_commands.describe(code="The code shown on the claude.com login page")
    async def logincode_cmd(interaction, code: str):
        await run_cmd(interaction, handler._logincode(code))

    @tree.command(name="mem", description="Memory: see caps, grant a session more, or take it back")
    @app_commands.describe(action="status, grant, deny, revert or ceiling",
                           project="Project whose session the grant applies to",
                           size="New hard cap, e.g. 12G (or the new ceiling)",
                           soft="Optional soft cap; defaults to 2G under the hard cap")
    @app_commands.choices(action=[
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="grant", value="grant"),
        app_commands.Choice(name="deny", value="deny"),
        app_commands.Choice(name="revert", value="revert"),
        app_commands.Choice(name="ceiling", value="ceiling"),
    ])
    @app_commands.autocomplete(project=project_autocomplete)
    async def mem_cmd(interaction, action: str = "status", project: str = "",
                      size: str = "", soft: str = ""):
        await run_cmd(interaction, lambda: handler._mem(action, project, size, soft))

    @tree.command(name="sudo", description="Approve which project scripts a session may run as root")
    @app_commands.describe(action="list, approve or revoke",
                           project="Project the script belongs to",
                           script="Script path inside the project, e.g. tools/setup.sh",
                           confirm="Hash prefix shown by the review step")
    @app_commands.choices(action=[
        app_commands.Choice(name="list", value="list"),
        app_commands.Choice(name="approve", value="approve"),
        app_commands.Choice(name="revoke", value="revoke"),
    ])
    @app_commands.autocomplete(project=project_autocomplete)
    async def sudo_cmd(interaction, action: str, project: str = "", script: str = "",
                       confirm: str = ""):
        await run_cmd(interaction, lambda: handler._sudo(action, project, script, confirm))

    @tree.command(name="help", description="Show the command reference")
    async def help_cmd(interaction):
        await run_cmd(interaction, lambda: handler._help())

    # ── Background tasks ───────────────────────────────────────────────────
    _bg_started = {"trash": False, "watchdog": False, "jobs": False,
                   "sessions": False, "session_mem": False, "mem_request": False,
                   "session_idle": False, "power": False, "reaper": False,
                   "share": False}

    async def scope_reaper_loop():
        """Reap leftover processes from dead sessions (see reap_orphan_scopes).

        First pass shortly after startup — that's the one that catches leftovers
        from a crash or reboot-less outage while the bot was down — then every
        minute. Posts a short note when it actually killed something, since a
        reaped emulator or build daemon is worth knowing about.
        """
        await asyncio.sleep(20)
        while True:
            try:
                reaped = await asyncio.to_thread(reap_orphan_scopes, logger)
                if reaped:
                    await post_channel(
                        "🧹 Cleaned up processes left behind by ended sessions:\n"
                        + "\n".join(f"• `{r}`" for r in reaped))
            except Exception as e:
                logger.warning(f"Scope reaper error: {e}")
            await asyncio.sleep(60)

    async def trash_purge_loop():
        """Purge expired .trash/ projects now, then once a day."""
        while True:
            try:
                n = await asyncio.to_thread(purge_trash, cfg, logger)
                if n:
                    logger.info(f"Trash purge: removed {n} expired project(s)")
            except Exception as e:
                logger.warning(f"Trash purge loop error: {e}")
            await asyncio.sleep(24 * 3600)

    async def usage_watchdog_loop():
        """Alert the owner when 5-hour or weekly usage crosses its threshold.

        Fires once per crossing (re-arms only after usage falls 10 points back
        below the threshold, e.g. when the window resets). Uses the live probe;
        a tick where the probe fails is simply skipped.
        """
        state = {"a5": False, "aw": False}
        th5 = float(cfg.get("usage_alert_5h_pct", 85))
        thw = float(cfg.get("usage_alert_weekly_pct", 90))
        interval = max(60, int(cfg.get("usage_alert_interval_sec", 1800)))
        await asyncio.sleep(45)  # let things settle after boot
        while True:
            try:
                # to_thread: parsing ~/.claude/projects JSONL is heavy disk work;
                # on the event loop it would stall Discord heartbeats.
                st = await asyncio.to_thread(probe_rate_limits, logger)
                if st:
                    mention = f"<@{owner['id']}> " if owner["id"] else ""
                    if st["pct5"] >= th5 and not state["a5"]:
                        state["a5"] = True
                        reset = CommandHandler._fmt_reset(st["reset5"], st["now"])
                        await post_channel(
                            f"⚠️ {mention}**5-hour usage at {st['pct5']:.0f}%**. "
                            f"Resets {reset}. Ease up to avoid hitting the cap.")
                        logger.info(f"Watchdog: 5h alert fired at {st['pct5']:.0f}%")
                    elif st["pct5"] < th5 - 10:
                        state["a5"] = False
                    if st["pctw"] is not None:
                        if st["pctw"] >= thw and not state["aw"]:
                            state["aw"] = True
                            reset = CommandHandler._fmt_reset(st["resetw"], st["now"])
                            await post_channel(
                                f"🚨 {mention}**Weekly usage at {st['pctw']:.0f}%**. "
                                f"Resets {reset}. You're close to the weekly limit.")
                            logger.info(f"Watchdog: weekly alert fired at {st['pctw']:.0f}%")
                        elif st["pctw"] < thw - 10:
                            state["aw"] = False
            except Exception as e:
                logger.warning(f"Usage watchdog error: {e}")
            await asyncio.sleep(interval)

    async def job_watchdog_loop():
        """Alert when a launch.sh job dies or its /health stops answering.

        Checks every 60s. Death alerts fire when a job-* tmux session that was
        seen running disappears (includes the launch.log tail so the failure
        reason is visible from the phone). Health checks apply only to jobs
        with PORT>0 in launch.sh, after a 3-tick startup grace, and re-arm on
        recovery. A bot restart starts with a clean slate (no false alerts).
        """
        prev = set()
        ticks_alive = {}   # job session -> consecutive ticks seen (grace period)
        health_bad = {}    # job session -> alert already fired
        root = Path(cfg["projects_root"])
        await asyncio.sleep(30)
        while True:
            try:
                cur = set(await asyncio.to_thread(active_jobs))
                mention = f"<@{owner['id']}> " if owner["id"] else ""
                for j in prev - cur:
                    pname = j[len(JOB_PREFIX) + 1:]
                    tail = await asyncio.to_thread(
                        tail_lines, root / pname / "launch.log", 3)
                    detail = ("\n```\n" + "\n".join(tail) + "\n```") if tail else ""
                    await post_channel(f"⚠️ {mention}Job **{pname}** stopped.{detail}")
                    logger.info(f"Job watchdog: {pname} stopped")
                    ticks_alive.pop(j, None)
                    health_bad.pop(j, None)
                for j in cur:
                    ticks_alive[j] = ticks_alive.get(j, 0) + 1
                    if ticks_alive[j] < 3:
                        continue
                    pname = j[len(JOB_PREFIX) + 1:]
                    port = launch_port(root / pname / "launch.sh")
                    if not port:
                        continue
                    ok = await asyncio.to_thread(health_ok, port)
                    if not ok and not health_bad.get(j):
                        health_bad[j] = True
                        await post_channel(
                            f"🩺 {mention}Job **{pname}** is running but "
                            f"`/health` on port {port} isn't answering. "
                            f"Check `/output {pname}`."
                        )
                        logger.info(f"Job watchdog: {pname} health failed (port {port})")
                    elif ok:
                        health_bad[j] = False
                prev = cur
            except Exception as e:
                logger.warning(f"Job watchdog error: {e}")
            await asyncio.sleep(60)

    async def session_watchdog_loop():
        """Alert when a cp-* Claude Code session dies unexpectedly.

        Checks every 60s. Fires once per session death, showing the last
        captured pane output so the cause is visible from the phone.
        Bot-dev session (cp-claude-pm) is excluded — that one dying means
        the bot itself is gone and can't alert anyway.
        """
        bot_sname = session_name(cfg, "claude-pm")
        # Seed from disk: sessions that were alive at the last tick before this
        # bot stopped. Any of those missing now died while nobody was watching
        # (reboot, crash, power loss) — previously that went unreported.
        seen = _read_json(SEEN_FILE, {})
        prev: set = set(seen.get("sessions", []))
        first = True
        await asyncio.sleep(60)  # let sessions settle after boot
        while True:
            try:
                cur = set(await asyncio.to_thread(active_sessions, cfg))
                mention = f"<@{owner['id']}> " if owner["id"] else ""
                if first:
                    first = False
                    missed = sorted(s for s in prev - cur if s != bot_sname
                                    and not was_killed_intentionally(s))
                    if missed:
                        pfx = len(cfg["tmux_prefix"]) + 1
                        names = ", ".join(f"**{s[pfx:]}**" for s in missed)
                        when = (datetime.fromtimestamp(seen["at"]).strftime("%b %-d %-I:%M %p")
                                if seen.get("at") else "before the restart")
                        await post_channel(
                            f"💀 {mention}While the bot was down, these sessions "
                            f"ended: {names}. They were last seen running at {when}, "
                            "so this was probably a reboot, crash or power loss. "
                            "Files are untouched. `/revive <name>` restarts one.")
                        logger.info(f"Session watchdog: died while bot was down: {missed}")
                    prev &= cur
                for sname in prev - cur:
                    if sname == bot_sname:
                        continue
                    pname = sname[len(cfg["tmux_prefix"]) + 1:]  # strip "cp-"
                    if was_killed_intentionally(sname):
                        # /stop, /delete, /revive, /halt — not a death.
                        _session_pane_cache.pop(sname, None)
                        logger.info(f"Session watchdog: {pname} stopped "
                                    f"intentionally (no alert)")
                        continue
                    # Pane is already gone; last_pane is whatever we captured
                    # on the previous tick (stored below).
                    last = _session_pane_cache.get(sname, "")
                    cause, usage_limited = _classify_death(last)
                    proj_path = Path(cfg["projects_root"]) / pname
                    if usage_limited:
                        action = (f"Once the limit resets, `/revive {pname}` "
                                  f"restarts it.")
                    else:
                        action = f"Use `/revive {pname}` to restart it."
                    await post_channel(
                        f"💀 {mention}Session **{pname}** died.\n"
                        f"{cause}\n"
                        f"📁 Your files are safe — `{proj_path}` is untouched "
                        f"(a session dying never deletes the project).\n"
                        f"{action}"
                    )
                    logger.info(f"Session watchdog: {pname} died ("
                                f"{'usage-limit' if usage_limited else 'other'})")
                    _session_pane_cache.pop(sname, None)
                # Cache last pane output for each live session so we have it
                # if the session dies between ticks.
                for sname in cur:
                    if sname == bot_sname:
                        continue
                    try:
                        pane = await asyncio.to_thread(capture_pane, sname)
                        if pane.strip():
                            _session_pane_cache[sname] = pane
                    except Exception:
                        pass
                prev = cur
                await asyncio.to_thread(_write_json, SEEN_FILE,
                                        {"sessions": sorted(cur), "at": _now_sec()})
            except Exception as e:
                logger.warning(f"Session watchdog error: {e}")
            await asyncio.sleep(60)

    async def session_mem_watchdog_loop():
        """Warn on Discord while there's still time to act, not after the kill.

        Watches each session/job scope AND the shared slice. It alerts when the
        working set reaches session_mem_alert_soft_pct of the SOFT cap (where
        throttling starts), or when PSI shows the unit stalling on memory. The
        old rule (75% of the hard cap) sat exactly at the soft cap, so a session
        pinned there and swapping (webapp, 2026-10-15: 6.2G, swap pegged at
        2G) could thrash for hours without a warning, and nothing was logged.
        Once per streak; re-arms when the unit is clearly below again.
        """
        alerted: set = set()
        bot_sname = session_name(cfg, "claude-pm")
        soft_pct = float(cfg.get("session_mem_alert_soft_pct", 90))
        psi_th = float(cfg.get("session_mem_alert_psi", 10))
        await asyncio.sleep(90)  # let sessions settle after boot
        while True:
            try:
                names = set(await asyncio.to_thread(active_sessions, cfg)) \
                    | set(await asyncio.to_thread(active_jobs))
                names.discard(bot_sname)
                units = {f"{_scope_unit(n)}.scope": n for n in names}
                units[SESSIONS_SLICE] = None
                mention = f"<@{owner['id']}> " if owner["id"] else ""
                for unit, sname in units.items():
                    d = await asyncio.to_thread(unit_mem_detail, unit)
                    verdict = mem_alert_state(d, soft_pct, psi_th)
                    if verdict == "clear":
                        alerted.discard(unit)
                    if verdict != "alert" or unit in alerted:
                        continue
                    alerted.add(unit)
                    who = ("**All sessions together**" if sname is None else
                           f"Session **{sname[len(cfg['tmux_prefix']) + 1:]}**"
                           if sname.startswith(cfg["tmux_prefix"] + "-")
                           else f"Job **{sname[len(JOB_PREFIX) + 1:]}**")
                    why = (f"stalling on memory {d['psi']:.0f}% of the time "
                           f"(thrashing)" if d["psi"] >= psi_th
                           else "close to its soft cap, where throttling starts")
                    gib = 1024 ** 3
                    swap = f", {d['swap'] / gib:.1f} GB swapped" if d["swap"] > 64e6 else ""
                    hard = f" (killed at {d['max'] / gib:.0f} GB)" if d.get("max") else ""
                    text = (f"⚠️ {mention}{who}: {d['used'] / gib:.1f} GB of a "
                            f"{d['soft'] / gib:.0f} GB soft cap{swap}{hard}. It's {why}.\n"
                            "→ Tell it to shed load (its rules + `claude-memcheck` "
                            "cover how), or `/stop` / `/halt` it.\n"
                            "→ If the job genuinely needs more, it can ask "
                            "(`claude-memcheck --request <size> --reason ...`) and you "
                            "lift the cap with `/mem action:grant`.")
                    logger.info(f"Mem watchdog: {unit} used={d['used']} soft={d['soft']} "
                                f"swap={d['swap']} psi60={d['psi']}")
                    await post_channel(text)
                alerted &= set(units)  # forget units that have since ended
            except Exception as e:
                logger.warning(f"Session mem watchdog error: {e}")
            await asyncio.sleep(60)

    async def mem_request_loop():
        """Surface a session's "I need more memory" request on Discord.

        Sessions can't talk to Discord themselves, so claude-memcheck
        --request drops the ask in a state file and this loop carries it to the
        phone with the exact command to answer it. Posted once per request;
        /mem grant|deny closes it.
        """
        posted: set = set()
        await asyncio.sleep(20)
        while True:
            try:
                reqs = await asyncio.to_thread(read_mem_requests)
                for proj, r in sorted(reqs.items()):
                    key = (proj, r.get("at"))
                    if r.get("status") != "pending" or key in posted:
                        continue
                    posted.add(key)
                    mention = f"<@{owner['id']}> " if owner["id"] else ""
                    cur = r.get("cur_max") or "its cap"
                    logger.info(f"Mem request from {proj}: {r.get('ask_max')} "
                                f"({r.get('reason', '')[:120]})")
                    await post_channel(
                        f"🧠 {mention}**{proj}** is asking for more memory: "
                        f"**{r.get('ask_max', '?')}** (now on {cur}).\n"
                        f"> {r.get('reason') or 'No reason given.'}\n"
                        f"`/mem action:grant project:{proj} size:{r.get('ask_max', '')}` · "
                        f"`/mem action:deny project:{proj}` · `/mem action:status`")
                posted &= {(k, v.get("at")) for k, v in reqs.items()}
            except Exception as e:
                logger.warning(f"Mem request loop error: {e}")
            await asyncio.sleep(15)

    async def share_queue_loop():
        """Post files that sessions shared with `claude-share` to the channel.

        The session's own "send file" tool can't deliver (no claude.ai project
        thread), so this is how a render or report reaches the phone. The files
        stay in the project folder; take_share_requests vets every path.
        """
        await asyncio.sleep(10)
        while True:
            try:
                reqs = await asyncio.to_thread(take_share_requests, cfg)
                ch = (client.get_channel(channel_id_int)
                      or await client.fetch_channel(channel_id_int)) if reqs else None
                for project, files, caption, rejected in reqs:
                    logger.info(f"Share from {project}: {len(files)} file(s)"
                                f"{f', {len(rejected)} rejected' if rejected else ''}")
                    head = f"📎 **{project}**" + (f": {caption}" if caption else "")
                    if rejected:
                        head += "\n⚠️ Not posted: " + "; ".join(rejected)
                    try:
                        await ch.send(head[:1990], files=[discord.File(str(f))
                                                          for f in files])
                    except Exception as e:
                        logger.warning(f"Share upload failed for {project}: {e}")
                        await post_channel(f"⚠️ **{project}** tried to share "
                                           f"{len(files)} file(s), but the upload "
                                           f"failed: {e}")
            except Exception as e:
                logger.warning(f"Share queue loop error: {e}")
            await asyncio.sleep(5)

    async def session_idle_watchdog_loop():
        """Warn when a session has been parked with no conversation activity.

        Two reasons a stale session is worth surfacing on the phone:
          - it pins ~1GB of RAM (and a memory cap) doing nothing, and
          - once it sits past the OAuth access token's ~8h life, waking it can
            fail with "Not logged in · Please run /login" — which
            `claude remote-control` cannot satisfy. That's the 2026-08-27
            iconsuite lockout: idle ~38h, then unusable from Discord.
        So the alert nudges toward `/revive` (restarts it, keeps files and
        gets a recap), `/refresh` (new conversation) or `/stop` (reclaim the memory).

        Fires once per idle streak and re-arms when the session is used again.
        """
        alerted: set = set()
        hours = float(cfg.get("session_idle_alert_hours", 12) or 0)
        interval = max(60, int(cfg.get("session_idle_interval_sec", 1800)))
        if hours <= 0:
            logger.info("Idle watchdog disabled (session_idle_alert_hours=0)")
            return
        thresh = hours * 3600
        await asyncio.sleep(120)  # let sessions settle after boot
        while True:
            try:
                cur = set(await asyncio.to_thread(active_sessions, cfg))
                mention = f"<@{owner['id']}> " if owner["id"] else ""
                for sname in cur:
                    pname = sname[len(cfg["tmux_prefix"]) + 1:]
                    idle = await asyncio.to_thread(session_idle_sec, cfg, pname)
                    if idle is None:
                        continue  # no transcript yet — nothing to judge
                    if idle < thresh:
                        alerted.discard(sname)  # used again → re-arm
                        continue
                    if sname in alerted:
                        continue
                    alerted.add(sname)
                    used, _cap = await asyncio.to_thread(session_mem, sname)
                    mem = f" holding {used / 1e9:.1f} GB," if used else ""
                    stale_auth = ""
                    if idle >= 8 * 3600:
                        stale_auth = (
                            "\n🔑 It's been idle longer than the ~8h auth token "
                            "lives, so picking it up may fail with "
                            "*\"Please run /login\"* — `/revive` avoids that.")
                    await post_channel(
                        f"❗ {mention}Session **{pname}** has been idle "
                        f"{_fmt_idle(idle)} (no conversation activity),{mem} "
                        f"still running.{stale_auth}\n"
                        f"→ `/revive {pname}` to restart it (same conversation), "
                        f"`/refresh {pname}` for a brand-new one, or "
                        f"`/stop {pname}` to free the memory."
                    )
                    logger.info(f"Idle watchdog: {pname} idle {_fmt_idle(idle)}")
                alerted &= cur  # forget sessions that have since ended
            except Exception as e:
                logger.warning(f"Idle watchdog error: {e}")
            await asyncio.sleep(interval)

    async def power_watchdog_loop():
        """Ping the phone when the laptop drops to battery (see PowerMonitor)."""
        if not cfg.get("power_alert", True):
            logger.info("Power watchdog disabled (power_alert=false)")
            return
        if (await asyncio.to_thread(power_state))["on_ac"] is None:
            logger.info("Power watchdog: no AC adapter/battery found — not running")
            return
        mon = PowerMonitor(cfg.get("power_alert_battery_pcts") or (50, 25, 10))
        interval = max(5, int(cfg.get("power_check_interval_sec", 15)))
        while True:
            try:
                st = await asyncio.to_thread(power_state)
                mention = f"<@{owner['id']}> " if owner["id"] else ""
                for text in mon.update(st, _now_sec(), mention):
                    logger.info(f"Power watchdog: {text}")
                    await post_channel(text)
            except Exception as e:
                logger.warning(f"Power watchdog error: {e}")
            await asyncio.sleep(interval)

    _session_pane_cache: dict = {}

    # ── Events ─────────────────────────────────────────────────────────────
    @client.event
    async def on_ready():
        logger.info(f"Discord bot ready — logged in as {client.user}")
        try:
            ch = client.get_channel(channel_id_int) or await client.fetch_channel(channel_id_int)
            guild = discord.Object(id=ch.guild.id)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            logger.info(f"Synced {len(synced)} slash commands to guild {ch.guild.id}")
        except Exception as e:
            logger.error(f"Slash command sync failed: {e}")
        if not _bg_started["trash"]:
            _bg_started["trash"] = True
            client.loop.create_task(trash_purge_loop())
        if not _bg_started["watchdog"]:
            _bg_started["watchdog"] = True
            client.loop.create_task(usage_watchdog_loop())
        if not _bg_started["jobs"]:
            _bg_started["jobs"] = True
            client.loop.create_task(job_watchdog_loop())
        if not _bg_started["sessions"]:
            _bg_started["sessions"] = True
            client.loop.create_task(session_watchdog_loop())
        if not _bg_started["session_mem"]:
            _bg_started["session_mem"] = True
            client.loop.create_task(session_mem_watchdog_loop())
        if not _bg_started["mem_request"]:
            _bg_started["mem_request"] = True
            client.loop.create_task(mem_request_loop())
        if not _bg_started["share"]:
            _bg_started["share"] = True
            client.loop.create_task(share_queue_loop())
        if not _bg_started["session_idle"]:
            _bg_started["session_idle"] = True
            client.loop.create_task(session_idle_watchdog_loop())
        if not _bg_started["power"]:
            _bg_started["power"] = True
            client.loop.create_task(power_watchdog_loop())
        await asyncio.to_thread(ensure_tmux_server_scope, logger)
        if not _bg_started["reaper"]:
            _bg_started["reaper"] = True
            client.loop.create_task(scope_reaper_loop())

        # Announce sessions that survived the restart so they're not lost.
        # Guarded: on_ready also fires on Discord reconnects, not just startup.
        if _bg_started.get("announced"):
            return
        _bg_started["announced"] = True
        try:
            surviving = active_sessions(cfg)
            if surviving:
                prefix = cfg["tmux_prefix"] + "-"
                names = ", ".join(
                    s[len(prefix):] for s in surviving
                    if s.startswith(prefix)
                )
                if names:
                    await post_channel(
                        f"Bot restarted. Sessions still running: **{names}**\n"
                        f"Use `/list` to see details or `/open <name>` to reconnect."
                    )
        except Exception as e:
            logger.warning(f"on_ready session scan failed: {e}")

    @client.event
    async def on_interaction(interaction):
        """Taps and pop-ups on a /new card (custom_id "np|…")."""
        if interaction.type not in (discord.InteractionType.component,
                                    discord.InteractionType.modal_submit):
            return
        parsed = np_parse((interaction.data or {}).get("custom_id", ""))
        if not parsed:
            return
        verb, owner_id, _args = parsed
        uid = interaction.user.id
        if uid != owner_id:
            await interaction.response.send_message(
                "That card belongs to someone else; start your own with `/new`.",
                ephemeral=True)
            return
        try:
            if verb == "cancel":
                await interaction.response.edit_message(
                    content=handler.new_form_cancel(uid), view=None)
                return
            wiz = handler.new_form(uid)
            if wiz is None:
                await interaction.response.edit_message(
                    content="This card expired (the bot restarted). Run `/new` again; "
                            "nothing was created.", view=None)
                return
            if verb == "details":
                await interaction.response.send_modal(new_form_modal(uid, wiz))
                return
            if verb == "create":
                await interaction.response.edit_message(
                    content=f"⏳ Creating **{wiz.data['name']}** and opening a session…",
                    view=None)
                result = await handler.new_form_create(uid)
                if handler.new_form(uid) is wiz:          # refused: show the card again
                    await interaction.edit_original_response(
                        content=result + "\n\n" + new_form_text(wiz),
                        view=new_form_view(uid, wiz))
                    return
                for chunk in _smart_chunks(result, 1900):
                    await interaction.followup.send(chunk)
                return
            note = ""
            if verb == "type":
                handler.new_form_set_type(wiz, interaction.data.get("values", ["0"])[0])
            elif verb == "pick":
                wiz.data["np_pick"] = interaction.data.get("values", [""])[0]
            elif verb == "modal":
                note = handler.new_form_apply(wiz, modal_values(interaction.data))
            text = new_form_text(wiz)
            if note:
                text = f"⚠️ {note}\n\n" + text
            await interaction.response.edit_message(
                content=text[:1990], view=new_form_view(uid, wiz))
        except Exception as e:
            logger.exception(f"/new card {verb} failed")
            msg = f"❌ That didn't work: {e}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg[:1900], ephemeral=True)
                else:
                    await interaction.response.send_message(msg[:1900], ephemeral=True)
            except Exception:
                pass

    @client.event
    async def on_message(message):
        # Plain messages are only used to drive an active wizard conversation;
        # everything else goes through slash commands.
        if message.author == client.user:
            return
        if message.channel.id != channel_id_int:
            return
        remember_owner(message.author.id)
        content = message.content.strip()
        if not content:
            return
        if not handler.wizard_active(message.author.id):
            return

        logger.info(f"[wizard:{message.author}] {content!r}")
        attach = None
        try:
            async with message.channel.typing():
                response = await handler.wizard_input(message.author.id, content)
            # A wizard's final step may hand back images alongside the text
            # (/box returns its renders this way).
            if isinstance(response, tuple):
                response, attach = response
        except Exception as e:
            # Surface the failure instead of leaving the user hanging; keep
            # wizard state so they can retry or type `cancel`.
            logger.exception("Wizard step failed")
            response = f"❌ Wizard step failed: {e}\nRetry, or type `cancel` to reset."
        for chunk in _smart_chunks(response, 1900):
            await message.channel.send(chunk)
        await send_attachments(message.channel.send, attach)

    try:
        await client.start(token)
    except discord.LoginFailure:
        logger.error("Discord login failed — check your bot token.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Bot error: {e}")
        sys.exit(1)


# ── Message chunking ─────────────────────────────────────────────────────────

def _smart_chunks(text: str, limit: int = 1900) -> list:
    """Split text into Discord-safe chunks, preferring line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find the last newline within the limit
        cut = text.rfind("\n", 0, limit)
        if cut == -1 or cut < limit // 2:
            # No good newline — fall back to space
            cut = text.rfind(" ", 0, limit)
        if cut == -1 or cut < limit // 2:
            # No good break point — hard cut
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ── First-time setup ──────────────────────────────────────────────────────────

def run_setup():
    cfg = load_config()
    print("\n── Claude PM First-Time Setup ───────────────────\n")
    root = input(f"AI projects root [{cfg['projects_root']}]: ").strip()
    if root:
        cfg["projects_root"] = root
    print("\n(See README.md for how to create a Discord bot and get these values)\n")
    token = input("Discord bot token: ").strip()
    if token:
        cfg["discord_token"] = token
    channel = input("Discord channel ID: ").strip()
    if channel:
        cfg["discord_channel_id"] = channel
    save_config(cfg)
    Path(cfg["projects_root"]).mkdir(parents=True, exist_ok=True)
    print(f"\n✅ Config saved to {CONFIG_FILE}")
    print(f"✅ Projects root: {cfg['projects_root']}")
    print("\nStart the daemon:  python3 claude_pm.py")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Claude Project Manager — Discord bot")
    parser.add_argument("--setup", action="store_true", help="First-time configuration")
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    cfg    = load_config()
    logger = setup_logging(cfg["log_file"])
    logger.info("Starting Claude PM...")
    Path(cfg["projects_root"]).mkdir(parents=True, exist_ok=True)
    install_global_rules(logger)

    try:
        asyncio.run(run_bot(cfg, logger))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down.")


if __name__ == "__main__":
    main()
