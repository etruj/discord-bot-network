#!/usr/bin/env python3
"""
Unit tests for claude_pm session management.

Tests cover:
- session_name / job_session_name naming
- session_exists detection
- active_sessions / active_jobs listing
- _extract_env_url parsing
- spawn_session tmux command construction (no pipe on stdin)
- trust_workspace writes correct JSON
"""

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

# Add parent dir to path so we can import claude_pm
sys.path.insert(0, str(Path(__file__).parent.parent))

import claude_pm

# Never let the suite touch the real on-disk state the live bot reads.
_STATE_TMP = Path(tempfile.mkdtemp(prefix="cpm-test-state-"))
claude_pm.STATE_DIR  = _STATE_TMP
claude_pm.KILLS_FILE = _STATE_TMP / "intentional_kills.json"
claude_pm.SEEN_FILE  = _STATE_TMP / "sessions_seen.json"
claude_pm.SLICE_FILE = _STATE_TMP / "claude-sessions.slice"
claude_pm.MEMREQ_FILE   = _STATE_TMP / "mem_requests.json"
claude_pm.MEMGRANT_FILE = _STATE_TMP / "mem_grants.json"
claude_pm.SHARE_QUEUE_DIR = _STATE_TMP / "share_queue"


class TestSessionNaming(unittest.TestCase):
    def test_session_name_uses_prefix(self):
        cfg = {"tmux_prefix": "cp"}
        self.assertEqual(claude_pm.session_name(cfg, "my-project"), "cp-my-project")

    def test_session_name_custom_prefix(self):
        cfg = {"tmux_prefix": "test"}
        self.assertEqual(claude_pm.session_name(cfg, "foo"), "test-foo")

    def test_job_session_name(self):
        self.assertEqual(claude_pm.job_session_name("my-project"), "job-my-project")

    def test_job_session_name_preserves_hyphens(self):
        self.assertEqual(claude_pm.job_session_name("my-cool-app"), "job-my-cool-app")


class TestSessionExists(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_session_exists_true(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(claude_pm.session_exists("cp-test"))
        mock_run.assert_called_once_with(
            ["tmux", "has-session", "-t", "=cp-test"],
            capture_output=True, timeout=10
        )

    @patch("claude_pm.subprocess.run")
    def test_session_exists_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(claude_pm.session_exists("cp-test"))


class TestActiveSessions(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_active_sessions_filters_by_prefix(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="cp-proj1\ncp-proj2\njob-proj1\nother-session\n"
        )
        cfg = {"tmux_prefix": "cp"}
        result = claude_pm.active_sessions(cfg)
        self.assertEqual(result, ["cp-proj1", "cp-proj2"])

    @patch("claude_pm.subprocess.run")
    def test_active_sessions_empty_when_no_tmux(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        cfg = {"tmux_prefix": "cp"}
        result = claude_pm.active_sessions(cfg)
        self.assertEqual(result, [])

    @patch("claude_pm.subprocess.run")
    def test_active_sessions_no_matching_prefix(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="job-foo\nother\n")
        cfg = {"tmux_prefix": "cp"}
        result = claude_pm.active_sessions(cfg)
        self.assertEqual(result, [])


class TestActiveJobs(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_active_jobs_filters_by_job_prefix(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="cp-proj1\njob-proj1\njob-proj2\nother\n"
        )
        result = claude_pm.active_jobs()
        self.assertEqual(result, ["job-proj1", "job-proj2"])

    @patch("claude_pm.subprocess.run")
    def test_active_jobs_empty_when_no_tmux(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = claude_pm.active_jobs()
        self.assertEqual(result, [])


class TestExtractEnvUrl(unittest.TestCase):
    def test_extracts_url_from_output(self):
        text = (
            "Connected · tetris_server · master\n"
            "Continue coding in the Claude app or "
            "https://claude.ai/code?environment=env_01EXAMPLEEXAMPLE\n"
            "space to show QR code"
        )
        url = claude_pm._extract_env_url(text)
        self.assertEqual(url, "https://claude.ai/code?environment=env_01EXAMPLEEXAMPLE")

    def test_returns_empty_when_no_url(self):
        text = "Workspace not trusted. Please run claude first."
        url = claude_pm._extract_env_url(text)
        self.assertEqual(url, "")

    def test_extracts_from_noisy_ansi_output(self):
        text = "[7A[J·✔︎· Connected\nhttps://claude.ai/code?environment=env_ABC123\nother stuff"
        url = claude_pm._extract_env_url(text)
        self.assertEqual(url, "https://claude.ai/code?environment=env_ABC123")


class TestSpawnSessionNoStdinPipe(unittest.TestCase):
    """Regression test: spawn_session must NOT pipe stdin (echo y |).
    It must use tmux send-keys to answer the prompt so the tty stays open."""

    def test_spawn_command_has_no_pipe(self):
        import ast
        import inspect
        source = inspect.getsource(claude_pm.spawn_session)
        # The source must not contain 'echo y |' which was the old broken approach
        self.assertNotIn("echo y |", source,
                         "spawn_session must not pipe stdin — use tmux send-keys instead")
        self.assertNotIn("echo y|", source)

    def test_spawn_uses_send_keys(self):
        import inspect
        source = inspect.getsource(claude_pm.spawn_session)
        self.assertIn("send-keys", source,
                       "spawn_session must use tmux send-keys to answer the enable prompt")


class TestNoModelPinning(unittest.TestCase):
    """The model is the app's choice (2026-10-20). The bot used to export
    ANTHROPIC_MODEL into every session, and an env var outranks the app — so a
    model picked in the Claude app didn't survive. Nothing here may set it."""

    def test_spawn_sets_no_model_env(self):
        import inspect
        source = inspect.getsource(claude_pm.spawn_session)
        # the assignment, not the word: the comment explains why it's gone
        self.assertNotIn("ANTHROPIC_MODEL=", source)
        self.assertNotIn("--model", source)

    def test_no_model_default_in_config(self):
        self.assertNotIn("model", claude_pm.DEFAULT_CONFIG)

    def test_nothing_else_pins_a_model_for_sessions(self):
        source = Path(claude_pm.__file__).read_text()
        self.assertNotIn("ANTHROPIC_MODEL=", source)
        self.assertNotIn("ANTHROPIC_SMALL_FAST_MODEL=", source)


class TestTrustWorkspace(unittest.TestCase):
    def test_trust_workspace_creates_entry(self, ):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"projects": {}}, f)
            tmp_path = Path(f.name)

        original = claude_pm.CLAUDE_JSON
        try:
            claude_pm.CLAUDE_JSON = tmp_path
            claude_pm.trust_workspace("/home/test/my-project")
            data = json.loads(tmp_path.read_text())
            self.assertTrue(data["projects"]["/home/test/my-project"]["hasTrustDialogAccepted"])
        finally:
            claude_pm.CLAUDE_JSON = original
            tmp_path.unlink()

    def test_trust_workspace_preserves_existing(self):
        import tempfile
        existing = {
            "someKey": "someValue",
            "projects": {
                "/home/test/old": {"hasTrustDialogAccepted": True, "other": 42}
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(existing, f)
            tmp_path = Path(f.name)

        original = claude_pm.CLAUDE_JSON
        try:
            claude_pm.CLAUDE_JSON = tmp_path
            claude_pm.trust_workspace("/home/test/new-project")
            data = json.loads(tmp_path.read_text())
            # Old entry preserved
            self.assertEqual(data["someKey"], "someValue")
            self.assertTrue(data["projects"]["/home/test/old"]["hasTrustDialogAccepted"])
            self.assertEqual(data["projects"]["/home/test/old"]["other"], 42)
            # New entry added
            self.assertTrue(data["projects"]["/home/test/new-project"]["hasTrustDialogAccepted"])
        finally:
            claude_pm.CLAUDE_JSON = original
            tmp_path.unlink()


class TestKillSession(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_kill_session_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(claude_pm.kill_session("cp-test"))

    @patch("claude_pm.subprocess.run")
    def test_kill_session_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(claude_pm.kill_session("cp-test"))


class TestBoxOptionParsing(unittest.TestCase):
    def _opt(self, key):
        return next(o for o in claude_pm.BOX_STEPS + claude_pm.BOX_ADVANCED
                    if o["key"] == key)

    def test_int_in_range(self):
        val, err = claude_pm._box_parse(self._opt("Width"), "160")
        self.assertEqual(val, 160)
        self.assertIsNone(err)

    def test_int_out_of_range_rejected(self):
        val, err = claude_pm._box_parse(self._opt("Width"), "999")
        self.assertIsNone(val)
        self.assertIn("20", err)

    def test_non_numeric_rejected(self):
        val, err = claude_pm._box_parse(self._opt("Width"), "wide")
        self.assertIsNone(val)

    def test_bool_forms(self):
        opt = self._opt("Handle")
        for yes in ("y", "yes", "YES", "true", "1"):
            self.assertIs(claude_pm._box_parse(opt, yes)[0], True)
        for no in ("n", "no", "false", "0"):
            self.assertIs(claude_pm._box_parse(opt, no)[0], False)
        self.assertIsNotNone(claude_pm._box_parse(opt, "maybe")[1])

    def test_choice_is_case_insensitive_and_canonical(self):
        opt = self._opt("Lip_Seal_Type")
        self.assertEqual(claude_pm._box_parse(opt, "SQUARE")[0], "square")
        self.assertIsNotNone(claude_pm._box_parse(opt, "hexagon")[1])


class TestBoxCliArgs(unittest.TestCase):
    def test_bools_use_no_prefix_when_false(self):
        args = claude_pm._box_cli_args({"Handle": True, "Label": False})
        self.assertIn("--handle", args)
        self.assertIn("--no-label", args)

    def test_values_are_flag_value_pairs(self):
        args = claude_pm._box_cli_args({"Width": 160, "Latch_Type": "clip"})
        self.assertEqual(args[args.index("--width") + 1], "160")
        self.assertEqual(args[args.index("--latch-type") + 1], "clip")

    def test_underscores_become_hyphens(self):
        args = claude_pm._box_cli_args({"Bottom_Height": 45})
        self.assertIn("--bottom-height", args)


class TestBoxWizardFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wiz = claude_pm.BoxWizard({"projects_root": self.tmp},
                                       logging.getLogger("test"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *messages):
        out = None
        for m in messages:
            out = asyncio.run(self.wiz.step(m))
        return out

    def test_label_text_only_asked_when_label_enabled(self):
        self.wiz.start()
        # name, W, L, base, lid, latches, style, handle, label=no -> confirm
        out = self._run("b1", "160", "100", "45", "18", "2", "-", "no", "no")
        self.assertIn("ready to build", out)
        self.assertEqual(self.wiz.state, "confirm")

    def test_label_yes_adds_a_step(self):
        self.wiz.start()
        out = self._run("b2", "160", "100", "45", "18", "2", "-", "no", "yes")
        self.assertIn("Label text", out)
        out = self._run("TOOLS")
        self.assertIn("ready to build", out)
        self.assertEqual(self.wiz.params["Label_Text"], "TOOLS")

    def test_dash_keeps_the_default(self):
        self.wiz.start()
        self._run("b3", "-", "-", "-", "-", "-", "-", "-", "-")
        self.assertEqual(self.wiz.params["Width"], claude_pm.BOX_DEFAULTS["Width"])

    def test_bad_answer_reasks_without_advancing(self):
        self.wiz.start()
        self._run("b4")
        before = self.wiz.idx
        self._run("enormous")
        self.assertEqual(self.wiz.idx, before)

    def test_advance_table_sets_only_the_chosen_option(self):
        self.wiz.start()
        self._run("b5", "160", "100", "45", "18", "2", "-", "no", "no")
        self._run("advance")
        self.assertEqual(self.wiz.state, "advance")
        self._run("1 square")
        self.assertEqual(self.wiz.params["Lip_Seal_Type"], "square")
        self.assertEqual(self.wiz.params["Wall_Thickness"],
                         claude_pm.BOX_DEFAULTS["Wall_Thickness"])
        out = self._run("done")
        self.assertIn("ready to build", out)

    def test_clasps_default_to_two(self):
        self.assertEqual(claude_pm.BOX_DEFAULTS["Latch_Count"], "2")

    def test_auto_is_a_clasp_choice_and_zero_is_not(self):
        opt = next(o for o in claude_pm.BOX_STEPS if o["key"] == "Latch_Count")
        self.assertIn(claude_pm.BOX_LATCH_AUTO, opt["choices"])
        self.assertNotIn("0", opt["choices"])

    def test_summary_spells_out_auto(self):
        self.wiz.start()
        out = self._run("b8", "160", "100", "45", "18", "auto", "-", "no", "no")
        self.assertIn("auto", out)
        self.assertIn("engine picks", out)

    def test_edit_changes_an_answer_and_returns_to_summary(self):
        self.wiz.start()
        self._run("b9", "160", "100", "45", "18", "2", "-", "no", "no")
        self._run("edit")
        self.assertEqual(self.wiz.state, "edit")
        self._run("2 200")                       # row 1 is the name, 2 is Width
        self.assertEqual(self.wiz.params["Width"], 200)
        self.assertEqual(self.wiz.params["Length"], 100)   # untouched
        out = self._run("done")
        self.assertIn("ready to build", out)
        self.assertIn("200", out)

    def test_edit_can_rename_the_box(self):
        self.wiz.start()
        self._run("oldname", "160", "100", "45", "18", "2", "-", "no", "no")
        self._run("edit", "1 newname")
        self.assertEqual(self.wiz.name, "newname")

    def test_edit_rejects_duplicate_rename(self):
        (Path(self.tmp) / claude_pm.BOXES_DIRNAME / "taken").mkdir(parents=True)
        self.wiz.start()
        self._run("mine", "160", "100", "45", "18", "2", "-", "no", "no")
        out = self._run("edit", "1 taken")
        self.assertIn("already exists", out)
        self.assertEqual(self.wiz.name, "mine")

    def test_edit_enabling_label_adds_its_text_row(self):
        self.wiz.start()
        self._run("b10", "160", "100", "45", "18", "2", "-", "no", "no")
        rows_before = len(self.wiz._edit_rows())
        self._run("edit", "9 yes")               # row 9 == Label
        self.assertTrue(self.wiz.params["Label"])
        self.assertEqual(len(self.wiz._edit_rows()), rows_before + 1)

    def test_edit_rejects_out_of_range_number(self):
        self.wiz.start()
        self._run("b11", "160", "100", "45", "18", "2", "-", "no", "no", "edit")
        out = self._run("99 nope")
        self.assertIn("Pick a number", out)

    def test_advance_rejects_out_of_range_number(self):
        self.wiz.start()
        self._run("b6", "160", "100", "45", "18", "2", "-", "no", "no", "advance")
        out = self._run("99 nope")
        self.assertIn("Pick a number", out)

    def test_duplicate_box_name_rejected(self):
        (Path(self.tmp) / claude_pm.BOXES_DIRNAME / "taken").mkdir(parents=True)
        self.wiz.start()
        out = self._run("taken")
        self.assertIn("already exists", out)
        self.assertEqual(self.wiz.state, "name")

    def test_cancel_resets(self):
        self.wiz.start()
        self._run("b7", "cancel")
        self.assertFalse(self.wiz.is_active())


class TestBoxesDirHiddenFromProjects(unittest.TestCase):
    def test_boxes_dir_is_not_listed_as_a_project(self):
        tmp = tempfile.mkdtemp()
        try:
            (Path(tmp) / claude_pm.BOXES_DIRNAME).mkdir()
            (Path(tmp) / "real-project").mkdir()
            (Path(tmp) / ".hidden").mkdir()
            handler = claude_pm.CommandHandler({"projects_root": tmp},
                                              logging.getLogger("test"))
            self.assertEqual(handler.project_names(), ["real-project"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestListUptime(unittest.TestCase):
    def _list_with(self, active, uptimes, idles=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (Path(tmp) / "live").mkdir()
        (Path(tmp) / "idle").mkdir()
        cfg = {"projects_root": tmp, "tmux_prefix": "cp",
               "session_idle_alert_hours": 12}
        handler = claude_pm.CommandHandler(cfg, logging.getLogger("test"))
        idles = idles or {}
        with patch.object(claude_pm, "active_sessions", return_value=active), \
             patch.object(claude_pm, "active_jobs", return_value=[]), \
             patch.object(claude_pm, "session_uptimes", return_value=uptimes), \
             patch.object(claude_pm, "session_idle_sec",
                          side_effect=lambda c, p: idles.get(p)):
            return handler._list()

    def _line(self, out, project):
        return [l for l in out.splitlines() if f"`{project}`" in l][0]

    def test_running_session_shows_uptime(self):
        out = self._list_with(["cp-live"], {"cp-live": 3600 * 2 + 300})
        self.assertIn("🟢  `live` — up 2h05m", out)

    def test_stopped_project_shows_no_uptime(self):
        # A ⚫ project has no tmux session, so "up 0m" would be a lie.
        out = self._list_with(["cp-live"], {"cp-live": 60})
        stopped = self._line(out, "idle")
        self.assertIn("⚫", stopped)
        self.assertNotIn("up ", stopped)

    def test_recently_used_session_hides_idle(self):
        # Under LIST_IDLE_MIN_SEC the session is obviously active — printing
        # "idle 2m" on every row is the clutter we're avoiding.
        out = self._list_with(["cp-live"], {"cp-live": 7200}, {"live": 120})
        self.assertIn("up 2h00m", self._line(out, "live"))
        self.assertNotIn("idle", self._line(out, "live"))

    def test_idle_shown_without_flag_below_alert_threshold(self):
        out = self._list_with(["cp-live"], {"cp-live": 86400},
                              {"live": 3 * 3600 + 900})
        line = self._line(out, "live")
        self.assertIn("· idle 3h15m", line)
        self.assertNotIn("❗", line)

    def test_stale_session_is_flagged_and_explained(self):
        out = self._list_with(["cp-live"], {"cp-live": 86400 * 2},
                              {"live": 22 * 3600})
        self.assertIn("· ❗ idle 22h00m", self._line(out, "live"))
        self.assertIn("❗ = stale", out)  # legend appears only when flagged

    def test_legend_omits_stale_key_when_nothing_is_stale(self):
        out = self._list_with(["cp-live"], {"cp-live": 7200}, {"live": 60})
        self.assertNotIn("❗", out)

    def test_idle_is_clamped_to_session_age(self):
        # /revive starts a fresh session on a project whose transcript is
        # older; "up 2h · idle 3h" would read as a bug.
        out = self._list_with(["cp-live"], {"cp-live": 7200},
                              {"live": 3 * 3600 + 900})
        self.assertIn("up 2h00m · idle 2h00m", self._line(out, "live"))

    def test_unknown_idle_degrades_to_uptime_only(self):
        out = self._list_with(["cp-live"], {"cp-live": 7200}, {})
        self.assertIn("up 2h00m", self._line(out, "live"))
        self.assertNotIn("idle", self._line(out, "live"))


class TestClassifyDeath(unittest.TestCase):
    def test_usage_limit_is_flagged(self):
        pane = "You've hit your session limit · resets 8:50pm (America/New_York)"
        cause, usage_limited = claude_pm._classify_death(pane)
        self.assertTrue(usage_limited)
        self.assertIn("usage limit", cause)

    def test_error_line_is_surfaced(self):
        pane = "doing work\nError: mesh boolean failed on face 12\n"
        cause, usage_limited = claude_pm._classify_death(pane)
        self.assertFalse(usage_limited)
        self.assertIn("mesh boolean failed", cause)

    def test_clean_idle_pane_reads_as_network_drop(self):
        pane = "All three v8 images are regenerated with the caps.\n"
        cause, usage_limited = claude_pm._classify_death(pane)
        self.assertFalse(usage_limited)
        self.assertIn("network", cause.lower())

    def test_empty_pane_does_not_crash(self):
        cause, usage_limited = claude_pm._classify_death("")
        self.assertFalse(usage_limited)
        self.assertTrue(cause)


class TestIntentionalKillSuppression(unittest.TestCase):
    def setUp(self):
        claude_pm._intentional_kills.clear()
        claude_pm.KILLS_FILE.unlink(missing_ok=True)

    def test_kill_session_records_intent(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            claude_pm.kill_session("cp-demo")
        self.assertTrue(claude_pm.was_killed_intentionally("cp-demo"))

    def test_record_is_consumed_once(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            claude_pm.kill_session("cp-demo")
        self.assertTrue(claude_pm.was_killed_intentionally("cp-demo"))
        # A later genuine death of the same project must still alert.
        self.assertFalse(claude_pm.was_killed_intentionally("cp-demo"))

    def test_unrelated_session_is_not_suppressed(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            claude_pm.kill_session("cp-demo")
        self.assertFalse(claude_pm.was_killed_intentionally("cp-other"))

    def test_stale_record_expires(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            claude_pm.kill_session("cp-demo")
        claude_pm._intentional_kills["cp-demo"] -= (
            claude_pm.INTENTIONAL_KILL_TTL + 1)
        kills = claude_pm._read_json(claude_pm.KILLS_FILE, {})
        kills["cp-demo"] -= claude_pm.INTENTIONAL_KILL_TTL + 1
        claude_pm._write_json(claude_pm.KILLS_FILE, kills)
        self.assertFalse(claude_pm.was_killed_intentionally("cp-demo"))

    def test_kill_by_another_process_is_seen(self):
        """BoxBot kills cp-boxbot from its own process: only the on-disk
        record reaches this bot's watchdog."""
        claude_pm._record_kill("cp-boxbot")
        self.assertTrue(claude_pm.was_killed_intentionally("cp-boxbot"))
        self.assertFalse(claude_pm.was_killed_intentionally("cp-boxbot"))


class TestOAuthTokenRefresh(unittest.TestCase):
    def test_env_var_wins_and_skips_refresh(self):
        with patch.dict("os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "env-tok"}), \
             patch.object(claude_pm, "_refresh_oauth_token") as refresh:
            self.assertEqual(claude_pm._oauth_token(), "env-tok")
            refresh.assert_not_called()

    def test_valid_token_is_used_without_refresh(self):
        future = claude_pm._now_sec() + 3600
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(claude_pm, "_token_expires_at", return_value=future), \
             patch.object(claude_pm, "_read_token_file", return_value="good"), \
             patch.object(claude_pm, "_refresh_oauth_token") as refresh:
            self.assertEqual(claude_pm._oauth_token(), "good")
            refresh.assert_not_called()

    def test_expired_token_triggers_refresh(self):
        past = claude_pm._now_sec() - 10
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(claude_pm, "_token_expires_at", return_value=past), \
             patch.object(claude_pm, "_read_token_file", return_value="stale"), \
             patch.object(claude_pm, "_refresh_oauth_token",
                          return_value="fresh") as refresh:
            self.assertEqual(claude_pm._oauth_token(), "fresh")
            refresh.assert_called_once()

    def test_failed_refresh_falls_back_to_stored_token(self):
        past = claude_pm._now_sec() - 10
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(claude_pm, "_token_expires_at", return_value=past), \
             patch.object(claude_pm, "_read_token_file", return_value="stale"), \
             patch.object(claude_pm, "_refresh_oauth_token", return_value=""):
            self.assertEqual(claude_pm._oauth_token(), "stale")

    def test_refreshes_a_full_cycle_ahead_of_expiry(self):
        # Token still valid but inside REFRESH_AHEAD_SEC → renew proactively so
        # the on-disk token never actually reaches expiry between probes.
        soon = claude_pm._now_sec() + claude_pm.REFRESH_AHEAD_SEC - 60
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(claude_pm, "_token_expires_at", return_value=soon), \
             patch.object(claude_pm, "_read_token_file", return_value="stale"), \
             patch.object(claude_pm, "_refresh_oauth_token",
                          return_value="fresh") as refresh:
            self.assertEqual(claude_pm._oauth_token(), "fresh")
            refresh.assert_called_once()


class TestOAuthRefreshGrant(unittest.TestCase):
    def _tmp_creds(self, oauth):
        p = Path(tempfile.mkdtemp()) / ".credentials.json"
        p.write_text(json.dumps({"claudeAiOauth": oauth}))
        return p

    def test_persist_merges_new_tokens_and_preserves_other_fields(self):
        p = self._tmp_creds({"accessToken": "old", "refreshToken": "r0",
                             "expiresAt": 1, "subscriptionType": "pro"})
        with patch.object(claude_pm, "CREDENTIALS_FILE", p):
            tok = claude_pm._persist_credentials(
                {"access_token": "new", "refresh_token": "r1",
                 "expires_in": 28800})
        self.assertEqual(tok, "new")
        o = json.loads(p.read_text())["claudeAiOauth"]
        self.assertEqual(o["accessToken"], "new")
        self.assertEqual(o["refreshToken"], "r1")          # rotated token saved
        self.assertEqual(o["subscriptionType"], "pro")     # untouched field kept
        self.assertGreater(o["expiresAt"], claude_pm._now_sec() * 1000)

    def test_grant_posts_refresh_and_persists(self):
        p = self._tmp_creds({"accessToken": "old", "refreshToken": "r0",
                             "expiresAt": 1})
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"access_token": "fresh", "refresh_token": "r1",
             "expires_in": 28800}).encode()
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        with patch.object(claude_pm, "CREDENTIALS_FILE", p), \
             patch.object(claude_pm, "_token_expires_at", return_value=0.0), \
             patch("urllib.request.urlopen", return_value=ctx) as urlopen:
            tok = claude_pm._oauth_refresh_grant()
        self.assertEqual(tok, "fresh")
        urlopen.assert_called_once()
        self.assertEqual(json.loads(p.read_text())["claudeAiOauth"]["refreshToken"],
                         "r1")

    def test_grant_skips_network_when_token_already_fresh(self):
        # Another refresher (or a fresh CLI login) renewed it while we waited on
        # the lock → no redundant rotation, no network call.
        p = self._tmp_creds({"accessToken": "cur", "refreshToken": "r0"})
        with patch.object(claude_pm, "CREDENTIALS_FILE", p), \
             patch.object(claude_pm, "_token_expires_at",
                          return_value=claude_pm._now_sec() + 99999), \
             patch.object(claude_pm, "_read_token_file", return_value="cur"), \
             patch("urllib.request.urlopen") as urlopen:
            tok = claude_pm._oauth_refresh_grant()
        self.assertEqual(tok, "cur")
        urlopen.assert_not_called()

    def test_grant_returns_empty_on_http_failure(self):
        p = self._tmp_creds({"accessToken": "old", "refreshToken": "r0"})
        with patch.object(claude_pm, "CREDENTIALS_FILE", p), \
             patch.object(claude_pm, "_token_expires_at", return_value=0.0), \
             patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertEqual(claude_pm._oauth_refresh_grant(), "")
        # File left intact — a failed refresh must never corrupt credentials.
        self.assertEqual(json.loads(p.read_text())["claudeAiOauth"]["accessToken"],
                         "old")


class TestIdleDetection(unittest.TestCase):
    def test_fmt_idle_units(self):
        f = claude_pm._fmt_idle
        self.assertEqual(f(90), "1m")
        self.assertEqual(f(3600), "1h")
        self.assertEqual(f(3600 * 3 + 1200), "3h 20m")
        self.assertEqual(f(86400), "1d")
        self.assertEqual(f(86400 * 2 + 3600 * 4), "2d 4h")

    def test_project_path_bot_dev_lives_outside_projects_root(self):
        cfg = {"projects_root": "/tmp/projects"}
        self.assertEqual(claude_pm.project_path(cfg, "foo"),
                         Path("/tmp/projects/foo"))
        # The bot-dev session lives in the bot's own repo, not projects_root.
        self.assertEqual(claude_pm.project_path(cfg, "claude-pm"),
                         Path(claude_pm.__file__).resolve().parent)

    def _fake_home(self, encoded, files):
        """Build a fake ~/.claude/projects/<encoded>/ with {name: age_sec}."""
        home = Path(tempfile.mkdtemp())
        d = home / ".claude" / "projects" / encoded
        (d / "sub" / "subagents").mkdir(parents=True)
        now = claude_pm._now_sec()
        for name, age in files.items():
            f = d / name
            f.write_text("{}")
            os.utime(f, (now - age, now - age))
        return home

    def test_idle_uses_newest_transcript(self):
        home = self._fake_home("-tmp-projects-foo",
                               {"old.jsonl": 9000, "new.jsonl": 600})
        with patch.object(claude_pm.Path, "home", return_value=home):
            idle = claude_pm.session_idle_sec({"projects_root": "/tmp/projects"},
                                              "foo")
        self.assertAlmostEqual(idle, 600, delta=30)  # newest wins

    def test_subagent_transcripts_count_as_activity(self):
        # A session doing all its work in subagents must not look idle.
        home = self._fake_home("-tmp-projects-foo",
                               {"main.jsonl": 9000,
                                "sub/subagents/agent-1.jsonl": 120})
        with patch.object(claude_pm.Path, "home", return_value=home):
            idle = claude_pm.session_idle_sec({"projects_root": "/tmp/projects"},
                                              "foo")
        self.assertAlmostEqual(idle, 120, delta=30)

    def test_idle_is_none_when_no_transcript_dir(self):
        home = Path(tempfile.mkdtemp())
        with patch.object(claude_pm.Path, "home", return_value=home):
            self.assertIsNone(
                claude_pm.session_idle_sec({"projects_root": "/tmp/projects"},
                                           "nope"))


class TestResolveStackKey(unittest.TestCase):
    """Regression coverage: typing a type's own name/label used to silently
    fall through to the generic custom path instead of matching it — e.g.
    "OpenSCAD" didn't match, so its trimmed flow (and the other types'
    follow-up questions) never fired unless you typed the bare menu key.
    The single letters are the pre-2026-10-20 menu keys, kept as aliases."""

    def test_exact_key_case_insensitive(self):
        self.assertEqual(claude_pm._resolve_stack_key("4"), "4")
        self.assertEqual(claude_pm._resolve_stack_key("1"), "1")

    def test_openscad_matches_by_name(self):
        for text in ("openscad", "OpenSCAD", "OpenSCAD (3D print)", "open-scad", "SCAD"):
            self.assertEqual(claude_pm._resolve_stack_key(text), "8", text)

    def test_kicad_matches_by_name_and_alias(self):
        for text in ("kicad", "KiCad", "KiCad (PCB design)", "pcb", "PCB"):
            self.assertEqual(claude_pm._resolve_stack_key(text), "9", text)

    def test_gbdk_matches_by_name_and_alias(self):
        for text in ("gbdk", "gameboy", "gb", "game dev"):
            self.assertEqual(claude_pm._resolve_stack_key(text), "7", text)

    def test_ctf_halves_are_distinct(self):
        for text in ("ctf", "CTF", "CTF — solve a challenge", "playthrough"):
            self.assertEqual(claude_pm._resolve_stack_key(text), "5", text)
        for text in ("ctfdev", "ctf build", "ctf design", "challenge"):
            self.assertEqual(claude_pm._resolve_stack_key(text), "6", text)

    def test_re_matches_by_name_and_alias(self):
        for text in ("re", "RE", "rea", "reversing", "ghidra", "decompile"):
            self.assertEqual(claude_pm._resolve_stack_key(text), "4", text)

    def test_old_menu_letters_still_land(self):
        # A/B/C/D were the pre-condensation keys for OpenSCAD/GBDK/CTF/KiCad.
        for letter, key in (("A", "8"), ("B", "7"), ("C", "5"), ("D", "9")):
            self.assertEqual(claude_pm._resolve_stack_key(letter), key, letter)

    def test_unrecognized_text_falls_through_as_custom(self):
        self.assertEqual(claude_pm._resolve_stack_key("some-custom-stack"), "")
        self.assertEqual(claude_pm._resolve_stack_key(""), "")


class TestCondensedStacks(unittest.TestCase):
    """The five web/Node menu rows are one row plus a follow-up question now;
    naming a runtime has to still produce the stack the old row hard-coded."""

    def test_framework_resolves_to_old_rows_runtime(self):
        self.assertEqual(claude_pm.resolve_runtime("Web", "python fastapi"),
                         ("Python", "FastAPI", "pytest", "uvicorn main:app --reload"))
        self.assertEqual(claude_pm.resolve_runtime("Web", "next.js")[:2],
                         ("JavaScript", "Next.js"))
        self.assertEqual(claude_pm.resolve_runtime("Script", "bash")[0], "Bash")
        self.assertEqual(claude_pm.resolve_runtime("Native", "rust")[0], "Rust")

    def test_skipped_answer_falls_back_to_the_common_choice(self):
        self.assertEqual(claude_pm.resolve_runtime("Web", "")[:2], ("Python", "FastAPI"))
        self.assertEqual(claude_pm.resolve_runtime("Script", "")[0], "Python")
        self.assertEqual(claude_pm.resolve_runtime("Native", "nonsense")[0], "Rust")

    def test_naming_a_framework_picks_the_row_and_answers_it(self):
        key, prefill = claude_pm.resolve_stack_input("fastapi")
        self.assertEqual(claude_pm.STACKS[key][1], "Web")
        self.assertEqual(prefill, "fastapi")
        key, prefill = claude_pm.resolve_stack_input("rust")
        self.assertEqual(claude_pm.STACKS[key][1], "Native")

    def test_menu_choice_does_not_prefill(self):
        self.assertEqual(claude_pm.resolve_stack_input("4"), ("4", ""))

    def test_launch_type_is_inferred_not_asked(self):
        self.assertEqual(claude_pm.launch_type_for("Web"), "web")
        self.assertEqual(claude_pm.launch_type_for("RE"), "cli")
        for lang in ("CTF", "CTFDEV", "OpenSCAD", "KiCad", ""):
            self.assertEqual(claude_pm.launch_type_for(lang), "none", lang)


class TestProjectWizardFlow(unittest.IsolatedAsyncioTestCase):
    """End-to-end: the step machine must apply each type's short-circuits
    whether the user types the menu key or the type's own name, and must never
    ask more questions than its flow says it will."""

    async def _wizard(self, *answers):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wiz = claude_pm.ProjectWizard({"projects_root": tmp}, logging.getLogger("t"))
        wiz.start()
        out = ""
        for a in answers:
            out = await wiz.step(a)
        return wiz, out

    async def test_openscad_asks_material_then_confirms(self):
        wiz, out = await self._wizard("proj", "desc", "OpenSCAD")
        self.assertEqual(wiz.state, "stack_detail")
        self.assertIn("material", out.lower())
        wiz, out = await self._wizard("proj", "desc", "A", "PETG")
        self.assertEqual(wiz.state, "confirm")          # 4 questions, no goals/rules
        self.assertEqual(wiz.data["stack_detail"], "PETG")

    async def test_ctf_solve_stops_at_the_category(self):
        wiz, _ = await self._wizard("proj", "desc", "ctf", "crypto")
        self.assertEqual(wiz.state, "confirm")
        self.assertEqual(wiz.data["stack_detail"], "crypto")

    async def test_re_flow_asks_target_then_goals_and_rules(self):
        wiz, out = await self._wizard("proj", "desc", "re")
        self.assertEqual(wiz.state, "stack_detail")
        self.assertIn("target", out.lower())
        wiz, _ = await self._wizard("proj", "desc", "re", "iOS ipa, armv7", "port it")
        self.assertEqual(wiz.state, "rules")
        wiz, _ = await self._wizard("proj", "desc", "re", "ipa", "port it", "skip")
        self.assertEqual(wiz.state, "confirm")
        self.assertEqual(wiz.data["rules"], "")

    async def test_ctf_build_on_a_gameboy_routes_to_the_gbc_playbook(self):
        wiz, _ = await self._wizard("proj", "desc", "ctf build", "gb, hidden code")
        self.assertEqual(claude_pm.STACKS[wiz.data["stack_key"]][1], "GBDK")
        self.assertTrue(wiz.data["stack_detail"].startswith("ctf,"))

    async def test_naming_a_framework_skips_the_follow_up(self):
        wiz, _ = await self._wizard("proj", "desc", "fastapi")
        self.assertEqual(wiz.state, "goals")            # detail already answered
        self.assertEqual(wiz.data["stack_detail"], "fastapi")

    async def test_unmatched_text_falls_back_to_the_generic_flow(self):
        wiz, _ = await self._wizard("proj", "desc", "some-custom-stack")
        self.assertEqual(wiz.state, "goals")
        self.assertEqual(wiz.data["stack_key"], "0")
        self.assertEqual(wiz.data["custom_stack"], "some-custom-stack")

    async def test_no_project_is_asked_about_launch_scripts_or_sudo(self):
        # Those two steps are gone: every project gets tools/setup.sh and the
        # launch type is inferred from the stack.
        wiz, out = await self._wizard("proj", "desc", "1", "fastapi", "goals", "skip")
        self.assertEqual(wiz.state, "confirm")
        for word in ("sudo", "launch script", "Background service"):
            self.assertNotIn(word.lower(), out.lower())

    async def test_step_headers_count_the_real_flow(self):
        wiz, out = await self._wizard("proj", "desc", "re")
        self.assertIn("Step 4 of 6", out)
        wiz, out = await self._wizard("proj", "desc", "openscad")
        self.assertIn("Step 4 of 4", out)


class TestTcpCheck(unittest.TestCase):
    def test_reachable(self):
        with patch("claude_pm.socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__.return_value = MagicMock()
            ok, detail = claude_pm._tcp_check("discord.com")
        self.assertTrue(ok)
        self.assertEqual(detail, "reachable")

    def test_dns_failure(self):
        import socket
        with patch("claude_pm.socket.create_connection",
                   side_effect=socket.gaierror("Name or service not known")):
            ok, detail = claude_pm._tcp_check("nope.invalid")
        self.assertFalse(ok)
        self.assertIn("DNS failed", detail)

    def test_connection_refused(self):
        with patch("claude_pm.socket.create_connection",
                   side_effect=OSError("Connection refused")):
            ok, detail = claude_pm._tcp_check("discord.com")
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)


class TestServiceHealth(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_parses_key_value_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ActiveState=active\nSubState=running\nNRestarts=3\n")
        result = claude_pm._service_health("claude-pm.service")
        self.assertEqual(result, {"ActiveState": "active", "SubState": "running",
                                  "NRestarts": "3"})

    @patch("claude_pm.subprocess.run", side_effect=OSError("no systemctl"))
    def test_returns_empty_on_failure(self, mock_run):
        self.assertEqual(claude_pm._service_health("claude-pm.service"), {})


class TestAuthStatus(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_parses_logged_in_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"loggedIn": true, "email": "e@x.com"}')
        result = claude_pm._auth_status()
        self.assertTrue(result["loggedIn"])
        self.assertEqual(result["email"], "e@x.com")

    @patch("claude_pm.subprocess.run",
          side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=15))
    def test_returns_empty_on_timeout(self, mock_run):
        self.assertEqual(claude_pm._auth_status(), {})

    @patch("claude_pm.subprocess.run")
    def test_returns_empty_on_bad_json(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="not json")
        self.assertEqual(claude_pm._auth_status(), {})


class TestClaudeCliVersions(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_reads_installed_and_latest(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="2.1.195 (Claude Code)\n"),
            MagicMock(returncode=0, stdout="2.1.283\n"),
        ]
        installed, latest = claude_pm._claude_cli_versions()
        self.assertEqual(installed, "2.1.195")
        self.assertEqual(latest, "2.1.283")

    @patch("claude_pm.subprocess.run", side_effect=OSError("not found"))
    def test_both_empty_when_commands_missing(self, mock_run):
        installed, latest = claude_pm._claude_cli_versions()
        self.assertEqual((installed, latest), ("", ""))


class TestReboot(unittest.TestCase):
    def _handler(self):
        return claude_pm.CommandHandler({"projects_root": "/tmp"},
                                        logging.getLogger("test"))

    def test_no_confirm_shows_warning_without_acting(self):
        with patch("claude_pm.subprocess.run") as mock_run:
            out = self._handler()._reboot("")
        mock_run.assert_not_called()
        self.assertIn("confirm:REBOOT", out)

    def test_confirm_reboot_schedules_shutdown(self):
        with patch("claude_pm.subprocess.run",
                   return_value=MagicMock(returncode=0)) as mock_run:
            out = self._handler()._reboot("REBOOT")
        mock_run.assert_called_once_with(
            claude_pm.SUDO_SHUTDOWN_REBOOT,
            capture_output=True, text=True, timeout=10)
        self.assertIn("60 seconds", out)

    def test_confirm_cancel_aborts_pending_shutdown(self):
        with patch("claude_pm.subprocess.run",
                   return_value=MagicMock(returncode=0)) as mock_run:
            out = self._handler()._reboot("CANCEL")
        mock_run.assert_called_once_with(
            claude_pm.SUDO_SHUTDOWN_CANCEL,
            capture_output=True, text=True, timeout=10)
        self.assertIn("cancelled", out)

    def test_sudo_argv_matches_sudoers_rules(self):
        """The bot's root commands must be exactly what sudoers allows, or
        /reboot fails with 'a password is required'."""
        rules = claude_pm.SUDOERS_RULES
        for argv in (claude_pm.SUDO_SHUTDOWN_REBOOT, claude_pm.SUDO_SHUTDOWN_CANCEL):
            self.assertIn(" ".join(argv[2:]), rules)
        self.assertIn(" ".join(claude_pm.SUDO_NPM_UPDATE[2:3]) + " ^install -g", rules)
        self.assertNotIn("NOPASSWD: ALL", rules)

    def test_reboot_command_failure_is_reported(self):
        with patch("claude_pm.subprocess.run",
                   return_value=MagicMock(returncode=1, stderr="sudo: a password is required")):
            out = self._handler()._reboot("REBOOT")
        self.assertIn("failed", out.lower())


class TestRelogin(unittest.IsolatedAsyncioTestCase):
    async def test_start_relogin_extracts_url_from_doubled_pane(self):
        # tmux capture-pane doesn't fully strip the OSC-8 hyperlink escape the
        # CLI prints the URL with, so the same URL appears twice back-to-back.
        pane = ("Opening browser to sign in...\n"
                "If the browser didn't open, visit: "
                "https://claude.com/cai/oauth/authorize?code=trueXXXhttps://claude.com/cai/oauth/authorize?code=trueXXX\n"
                "Paste code here if prompted > ")
        with patch("claude_pm.session_exists", side_effect=[False, True]), \
             patch("claude_pm.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("claude_pm.capture_pane", return_value=pane):
            ok, msg = await claude_pm.start_relogin()
        self.assertTrue(ok)
        self.assertIn("https://claude.com/cai/oauth/authorize?code=trueXXX", msg)
        self.assertNotIn("trueXXXhttps", msg)

    async def test_submit_code_detects_success(self):
        with patch("claude_pm.session_exists", side_effect=[True, True]), \
             patch("claude_pm.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("claude_pm.capture_pane", return_value="Login successful."), \
             patch("claude_pm.kill_session") as mock_kill:
            ok, msg = await claude_pm.submit_relogin_code("ABC-123")
        self.assertTrue(ok)
        mock_kill.assert_called_once_with(claude_pm.RELOGIN_SESSION)

    async def test_submit_code_detects_invalid(self):
        with patch("claude_pm.session_exists", side_effect=[True, True]), \
             patch("claude_pm.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("claude_pm.capture_pane",
                  return_value="Invalid code. Please make sure the full code was copied."):
            ok, msg = await claude_pm.submit_relogin_code("wrong")
        self.assertFalse(ok)
        self.assertIn("Invalid code", msg)

    async def test_submit_code_without_active_session(self):
        with patch("claude_pm.session_exists", return_value=False):
            ok, msg = await claude_pm.submit_relogin_code("ABC-123")
        self.assertFalse(ok)
        self.assertIn("/relogin", msg)


class TestPowerState(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def _supply(self, name, **files):
        d = self.root / name
        d.mkdir()
        for k, v in files.items():
            (d / k).write_text(f"{v}\n")

    def test_adapter_online(self):
        self._supply("ADP1", type="Mains", online=1)
        self._supply("BAT0", type="Battery", capacity=107, status="Full")
        self.assertEqual(claude_pm.power_state(self.root),
                         {"on_ac": True, "pct": 100, "status": "Full"})

    def test_adapter_offline(self):
        self._supply("ADP1", type="Mains", online=0)
        self._supply("BAT0", type="Battery", capacity=80, status="Discharging")
        st = claude_pm.power_state(self.root)
        self.assertFalse(st["on_ac"])
        self.assertEqual(st["pct"], 80)

    def test_any_usb_c_adapter_counts(self):
        self._supply("ADP1", type="Mains", online=0)
        self._supply("ucsi-source-psy-1", type="USB", online=1)
        self.assertTrue(claude_pm.power_state(self.root)["on_ac"])

    def test_peripheral_battery_ignored(self):
        self._supply("hidpp_battery_0", type="Battery", scope="Device",
                     capacity=5, status="Discharging")
        self.assertIsNone(claude_pm.power_state(self.root)["on_ac"])

    def test_no_adapter_falls_back_to_battery_status(self):
        self._supply("BAT0", type="Battery", capacity=60, status="Discharging")
        self.assertFalse(claude_pm.power_state(self.root)["on_ac"])

    def test_missing_dir(self):
        st = claude_pm.power_state(self.root / "nope")
        self.assertIsNone(st["on_ac"])


class TestPowerMonitor(unittest.TestCase):
    AC  = {"on_ac": True,  "pct": 90}

    @staticmethod
    def bat(pct):
        return {"on_ac": False, "pct": pct}

    def test_plugged_in_is_quiet(self):
        m = claude_pm.PowerMonitor()
        self.assertEqual(m.update(self.AC, 0), [])
        self.assertEqual(m.update(self.AC, 15), [])

    def test_unplug_needs_two_readings(self):
        m = claude_pm.PowerMonitor()
        self.assertEqual(m.update(self.bat(90), 0, "<@1> "), [])
        msgs = m.update(self.bat(90), 15, "<@1> ")
        self.assertEqual(len(msgs), 1)
        self.assertIn("unplugged", msgs[0])
        self.assertIn("<@1>", msgs[0])
        self.assertEqual(m.update(self.bat(89), 30), [])  # no repeat

    def test_flicker_does_not_alert(self):
        m = claude_pm.PowerMonitor()
        self.assertEqual(m.update(self.bat(90), 0), [])
        self.assertEqual(m.update(self.AC, 15), [])
        self.assertEqual(m.update(self.bat(90), 30), [])
        self.assertEqual(m.update(self.AC, 45), [])

    def test_level_alerts_once_each(self):
        m = claude_pm.PowerMonitor((50, 25, 10))
        m.update(self.bat(60), 0); m.update(self.bat(60), 15)
        self.assertEqual(m.update(self.bat(51), 30), [])
        self.assertIn("50%", m.update(self.bat(50), 45)[0])
        self.assertEqual(m.update(self.bat(49), 60), [])
        self.assertIn("🔋", m.update(self.bat(24), 75)[0])
        self.assertIn("🪫", m.update(self.bat(9), 90)[0])
        self.assertEqual(m.update(self.bat(8), 105), [])

    def test_unplug_below_level_skips_already_passed_levels(self):
        m = claude_pm.PowerMonitor((50, 25, 10))
        m.update(self.bat(30), 0)
        self.assertEqual(len(m.update(self.bat(30), 15)), 1)  # unplug only
        self.assertEqual(m.update(self.bat(29), 30), [])
        self.assertEqual(len(m.update(self.bat(25), 45)), 1)

    def test_replug_all_clear_and_rearm(self):
        m = claude_pm.PowerMonitor((50,))
        m.update(self.bat(60), 0); m.update(self.bat(60), 15)
        m.update(self.bat(50), 30)
        m.update(self.AC, 400)
        msgs = m.update(self.AC, 415)
        self.assertIn("Power is back", msgs[0])
        self.assertIn("6m", msgs[0])
        m.update(self.bat(45), 500)
        self.assertEqual(len(m.update(self.bat(45), 515)), 1)  # unplug again

    def test_unknown_state_ignored(self):
        m = claude_pm.PowerMonitor()
        self.assertEqual(m.update({"on_ac": None, "pct": None}, 0), [])


class TestFreeScopeUnit(unittest.TestCase):
    """Regression tests for the 2026-10-15 /revive outage.

    A crash left claude-cp-webapp.scope active with children that ignored
    SIGTERM, so `systemctl stop` blocked past its 10s timeout; subprocess.run
    raised TimeoutExpired, which escaped spawn_session and killed /revive.
    """

    @staticmethod
    def _states(*seq):
        """Fake systemctl: LoadState reads come from `seq`, in order."""
        it = iter(seq)
        def fake(argv, **kw):
            if "show" in argv:
                return MagicMock(returncode=0, stdout=next(it) + "\n")
            return MagicMock(returncode=0, stdout="")
        return fake

    @patch("claude_pm.subprocess.run")
    def test_absent_scope_is_free_without_stopping(self, mock_run):
        mock_run.side_effect = self._states("not-found")
        self.assertTrue(claude_pm._free_scope_unit("claude-cp-x"))
        # The common path must not pay for a stop/kill it doesn't need.
        self.assertEqual(mock_run.call_count, 1)

    @patch("claude_pm.subprocess.run")
    def test_stop_is_non_blocking(self, mock_run):
        """The stop must use --no-block, or one SIGTERM-ignoring child holds
        the client for TimeoutStopSec — the original bug."""
        mock_run.side_effect = self._states("loaded", "not-found", "not-found")
        claude_pm._free_scope_unit("claude-cp-x")
        stops = [c for c in mock_run.call_args_list if "stop" in c.args[0]]
        self.assertTrue(stops, "expected a stop call")
        self.assertIn("--no-block", stops[0].args[0])

    @patch("claude_pm.subprocess.run")
    def test_timeout_does_not_propagate(self, mock_run):
        """TimeoutExpired anywhere in cleanup must not escape: it aborted the
        whole revive instead of degrading to an uncapped launch."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="systemctl", timeout=5)
        try:
            res = claude_pm._free_scope_unit("claude-cp-x", budget=0.1)
        except subprocess.TimeoutExpired:
            self.fail("_free_scope_unit leaked TimeoutExpired")
        self.assertFalse(res, "unreadable state must not be reported as free")

    @patch("claude_pm.subprocess.run")
    def test_escalates_to_sigkill_when_sigterm_ignored(self, mock_run):
        mock_run.side_effect = self._states(*(["loaded"] * 40))
        claude_pm._free_scope_unit("claude-cp-x", budget=0.4)
        kills = [c for c in mock_run.call_args_list if "kill" in c.args[0]]
        self.assertTrue(kills, "a scope that ignores SIGTERM must be SIGKILLed")
        self.assertIn("--signal=KILL", kills[0].args[0])

    @patch("claude_pm.subprocess.run")
    def test_unknown_load_state_is_not_free(self, mock_run):
        """Treating an unreadable state as free is what sends a launch down the
        'Unit was already loaded' path (instant death, empty pane)."""
        mock_run.side_effect = self._states(*(["bogus"] * 40))
        self.assertFalse(claude_pm._free_scope_unit("claude-cp-x", budget=0.3))


class TestMemScopePrefix(unittest.TestCase):
    @patch("claude_pm.shutil.which", return_value="/usr/bin/systemd-run")
    @patch("claude_pm._free_scope_unit", return_value=True)
    def test_caps_applied_when_name_free(self, _free, _which):
        argv = claude_pm._mem_scope_prefix(
            {"session_mem_max": "8G", "session_mem_high": "6G",
             "session_mem_swap_max": "2G"}, "cp-foo")
        self.assertEqual(argv[:5], ["systemd-run", "--user", "--scope",
                                    "--quiet", "--collect"])
        self.assertIn("--unit=claude-cp-foo", argv)
        self.assertIn("MemoryMax=8G", argv)
        self.assertIn("MemoryHigh=6G", argv)
        self.assertIn("MemorySwapMax=2G", argv)

    @patch("claude_pm.shutil.which", return_value="/usr/bin/systemd-run")
    @patch("claude_pm._free_scope_unit", return_value=False)
    def test_degrades_to_uncapped_when_name_stuck(self, _free, _which):
        """A wedged leftover must cost the cap, not the session: launching
        capped would abort outright."""
        self.assertEqual(
            claude_pm._mem_scope_prefix({"session_mem_max": "8G"}, "cp-foo"), [])

    @patch("claude_pm.shutil.which", return_value="/usr/bin/systemd-run")
    @patch("claude_pm._free_scope_unit")
    def test_disabled_cap_skips_scope_entirely(self, free, _which):
        self.assertEqual(
            claude_pm._mem_scope_prefix({"session_mem_max": ""}, "cp-foo"), [])
        free.assert_not_called()

    @patch("claude_pm.shutil.which", return_value=None)
    def test_no_systemd_run_available(self, _which):
        self.assertEqual(
            claude_pm._mem_scope_prefix({"session_mem_max": "8G"}, "cp-foo"), [])


class TestOrphanScopeReaper(unittest.TestCase):
    """The sweep must reap leftovers of dead sessions and NEVER a live one —
    including BoxBot's cp-boxbot, which it spawns through this module."""
    LIVE = {"claude-cp-webapp", "claude-cp-boxbot"}
    RC = [(1, "claude", "claude remote-control --name x --spawn=same-dir")]
    DAEMON = [(2, "netsimd", "/opt/android-sdk/emulator/netsimd")]

    def test_dead_session_leftovers_are_orphans(self):
        self.assertTrue(claude_pm._is_orphan_scope(
            "claude-cp-gone.scope", self.LIVE, self.DAEMON))

    def test_live_tmux_session_is_never_orphan(self):
        self.assertFalse(claude_pm._is_orphan_scope(
            "claude-cp-webapp.scope", self.LIVE, self.DAEMON))
        self.assertFalse(claude_pm._is_orphan_scope(
            "claude-cp-boxbot.scope", self.LIVE, self.DAEMON))

    def test_running_remote_control_is_never_orphan(self):
        """Second, independent signal: even with no tmux session mapping to
        it (tmux hiccup, naming mismatch), a live claude keeps its scope."""
        self.assertFalse(claude_pm._is_orphan_scope(
            "claude-cp-gone.scope", set(), self.RC + self.DAEMON))

    def test_empty_scope_is_orphan(self):
        self.assertTrue(claude_pm._is_orphan_scope("claude-cp-gone.scope", set(), []))

    @patch("claude_pm._reap", return_value="claude-cp-gone.scope: 1 process(es)")
    @patch("claude_pm._scope_procs")
    @patch("claude_pm._session_scopes",
           return_value=["claude-cp-webapp.scope", "claude-cp-gone.scope"])
    @patch("claude_pm._all_tmux_sessions", return_value={"cp-webapp"})
    def test_sweep_reaps_only_the_orphan(self, _tmux, _scopes, procs, reap):
        procs.side_effect = lambda sc: (self.RC if "webapp" in sc else self.DAEMON)
        out = claude_pm.reap_orphan_scopes()
        reap.assert_called_once()
        self.assertEqual(reap.call_args.args[0], "claude-cp-gone.scope")
        self.assertEqual(len(out), 1)

    @patch("claude_pm._reap")
    @patch("claude_pm._session_scopes", return_value=["claude-cp-webapp.scope"])
    @patch("claude_pm._all_tmux_sessions", return_value=set())
    @patch("claude_pm._scope_procs")
    def test_tmux_unreadable_still_spares_live_claude(self, procs, _t, _s, reap):
        """If `tmux list-sessions` fails, every scope looks session-less; the
        remote-control check alone must keep live sessions alive."""
        procs.return_value = self.RC
        self.assertEqual(claude_pm.reap_orphan_scopes(), [])
        reap.assert_not_called()

    @patch("claude_pm._scope_gone", return_value=True)
    @patch("claude_pm._reap")
    def test_reap_session_scope_noop_when_absent(self, reap, _gone):
        self.assertEqual(claude_pm.reap_session_scope("cp-x"), "")
        reap.assert_not_called()


class TestInstallGlobalRules(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.md, self.mc = self.d / "CLAUDE.md", self.d / "bin" / "claude-memcheck"
        self.addCleanup(shutil.rmtree, self.d)

    def install(self):
        return claude_pm.install_global_rules(dest=self.md, memcheck_dest=self.mc)

    def test_fresh_install(self):
        self.assertTrue(self.install())
        text = self.md.read_text()
        self.assertIn(claude_pm.RULES_BEGIN, text)
        self.assertIn("claude-memcheck", text)
        self.assertTrue(os.access(self.mc, os.X_OK))
        self.assertTrue(os.access(self.mc.parent / "sudo", os.X_OK))
        self.assertTrue(os.access(self.mc.parent / "claude-share", os.X_OK))
        self.assertIn("claude-share", text)

    def test_idempotent(self):
        self.install()
        self.assertFalse(self.install())

    def test_preserves_users_own_content(self):
        self.md.write_text("# My own global notes\n\nalways use tabs\n")
        self.install()
        self.install()
        text = self.md.read_text()
        self.assertTrue(text.startswith("# My own global notes"))
        self.assertIn("always use tabs", text)
        self.assertEqual(text.count(claude_pm.RULES_END), 1)

    def test_replaces_stale_block_in_place(self):
        self.md.write_text(f"before\n{claude_pm.RULES_BEGIN}\nOLD RULES\n"
                           f"{claude_pm.RULES_END}\nafter\n")
        self.assertTrue(self.install())
        text = self.md.read_text()
        self.assertNotIn("OLD RULES", text)
        self.assertTrue(text.startswith("before\n"))
        self.assertTrue(text.rstrip().endswith("after"))


class TestExactTmuxTargets(unittest.TestCase):
    """`-t cp-web` prefix-matched cp-webapp, so /stop web killed webapp."""
    def test_targets_are_exact(self):
        self.assertEqual(claude_pm._tx("cp-web"), "=cp-web")
        self.assertEqual(claude_pm._txp("cp-web"), "=cp-web:")

    @patch("claude_pm.subprocess.run")
    def test_kill_uses_exact_target(self, run):
        run.return_value = MagicMock(returncode=0)
        claude_pm.kill_session("cp-web")
        self.assertIn("=cp-web", run.call_args.args[0])

    @patch("claude_pm.subprocess.run")
    def test_capture_uses_exact_pane_target(self, run):
        run.return_value = MagicMock(stdout="")
        claude_pm.capture_pane("cp-web")
        self.assertIn("=cp-web:", run.call_args.args[0])


class TestProjectNameValidation(unittest.TestCase):
    def test_valid(self):
        for n in ("webapp", "WEB-app_2", "a"):
            self.assertEqual(claude_pm.bad_project_name(n), "", n)

    def test_invalid(self):
        for n in ("../claude-pm", "a/b", "a.b", "x:y", "", "-rf", ".trash", "a b"):
            self.assertNotEqual(claude_pm.bad_project_name(n), "", n)

    def test_delete_traversal_refused_and_nothing_moved(self):
        h = claude_pm.CommandHandler({"projects_root": "/nonexistent",
                                      "tmux_prefix": "cp"}, logging.getLogger("t"))
        with patch("claude_pm.shutil.move") as mv:
            out = h._delete("../claude-pm", "../claude-pm")
        self.assertIn("isn't a valid project name", out)
        mv.assert_not_called()

    def test_wizard_rejects_reserved_names(self):
        self.assertIn("claude-pm", claude_pm.RESERVED_NAMES)
        self.assertIn("boxbot", claude_pm.RESERVED_NAMES)


class TestMemAlertState(unittest.TestCase):
    G = 1e9
    def d(self, used, psi=0.0):
        return {"used": used * self.G, "soft": 6 * self.G, "max": 8 * self.G,
                "swap": 0, "psi": psi}

    def test_near_soft_cap_alerts(self):
        self.assertEqual(claude_pm.mem_alert_state(self.d(5.5), 90, 10), "alert")

    def test_thrashing_alerts_even_below_cap(self):
        """webapp: throttled at the soft cap, so RSS looked 'fine' while it
        stalled. PSI catches that."""
        self.assertEqual(claude_pm.mem_alert_state(self.d(4.0, psi=25), 90, 10), "alert")

    def test_clear_rearms(self):
        self.assertEqual(claude_pm.mem_alert_state(self.d(2.0, psi=0.5), 90, 10), "clear")

    def test_hysteresis_holds(self):
        self.assertEqual(claude_pm.mem_alert_state(self.d(5.0, psi=0.5), 90, 10), "")

    def test_no_data(self):
        self.assertEqual(claude_pm.mem_alert_state({}, 90, 10), "")


class TestSessionsSlice(unittest.TestCase):
    def setUp(self):
        claude_pm._slice_ok.update(cfg=None, ok=False)
        claude_pm.SLICE_FILE.unlink(missing_ok=True)
        self.addCleanup(claude_pm._slice_ok.update, cfg=None, ok=False)

    @patch("claude_pm._sctl", return_value=MagicMock(returncode=0))
    def test_writes_unit_and_reloads_once(self, sctl):
        cfg = {"sessions_total_mem_high": "10G", "sessions_total_mem_max": "12G",
               "sessions_total_swap_max": "3G"}
        self.assertTrue(claude_pm.ensure_sessions_slice(cfg))
        body = claude_pm.SLICE_FILE.read_text()
        self.assertIn("MemoryMax=12G", body)
        self.assertIn("MemoryHigh=10G", body)
        self.assertTrue(claude_pm.ensure_sessions_slice(cfg))
        self.assertEqual(sctl.call_count, 1)   # cached: no second daemon-reload

    def test_disabled(self):
        self.assertFalse(claude_pm.ensure_sessions_slice({"sessions_total_mem_max": ""}))

    @patch("claude_pm.shutil.which", return_value="/usr/bin/systemd-run")
    @patch("claude_pm._free_scope_unit", return_value=True)
    @patch("claude_pm.ensure_sessions_slice", return_value=True)
    def test_scope_joins_shared_slice(self, *_):
        argv = claude_pm._mem_scope_prefix({"session_mem_max": "8G"}, "cp-foo")
        self.assertIn("--slice=claude-sessions.slice", argv)


class TestResumeRecap(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        self.proj, self.tdir = self.d / "proj", self.d / "transcripts"
        self.proj.mkdir(); self.tdir.mkdir()

    def recap(self):
        with patch("claude_pm._transcript_dir", return_value=self.tdir):
            return claude_pm.build_resume_recap(self.proj)

    def test_log_and_last_exchange(self):
        (self.proj / "LOG.md").write_text(
            "# Log\n\n## 2026-10-15 — Ported the codec\nAudio works now.\n\n## older\nx\n")
        rows = [
            {"type": "user", "message": {"content": "port the audio codec"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done — codec ported."}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "message": {"content": "<system-reminder>noise</system-reminder>"}},
        ]
        (self.tdir / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        out = self.recap()
        self.assertIn("Ported the codec", out)
        self.assertIn("Audio works now.", out)
        self.assertIn("port the audio codec", out)
        self.assertIn("codec ported", out)
        self.assertNotIn("noise", out)

    def test_empty_project(self):
        self.assertEqual(self.recap(), "")


class TestRefreshSession(unittest.IsolatedAsyncioTestCase):
    """/refresh must yield a NEW conversation. /revive of myproj (2026-10-21)
    reattached to the old one because remote-control resumes whatever
    bridge-pointer.json names; /refresh moves it aside and retries while the
    server still holds the folder."""
    CFG = {"projects_root": "/x", "tmux_prefix": "cp", "revive_summary": False}
    SERVED = ("Session died unexpectedly.\n```\nError: This folder is already "
              "served by a terminal `claude remote-control` on this device.\n```")

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        self.proj, self.tdir = d / "proj", d / "transcripts"
        self.proj.mkdir(); self.tdir.mkdir()
        self.ptr = self.tdir / "bridge-pointer.json"
        self.ptr.write_text('{"sessionId":"session_old"}')
        for target, kw in [("session_dir", {"return_value": self.proj}),
                           ("_transcript_dir", {"return_value": self.tdir}),
                           ("session_exists", {"return_value": True}),
                           ("trust_workspace", {}), ("reap_session_scope", {}),
                           ("REFRESH_RETRY_SEC", {"new": 0})]:
            m = patch(f"claude_pm.{target}", **kw)
            setattr(self, target, m.start())
            self.addCleanup(m.stop)
        m = patch("claude_pm.kill_session", return_value=True)
        self.kill = m.start(); self.addCleanup(m.stop)

    async def test_pointer_moved_aside_and_retries_until_up(self):
        seen = []
        async def spawn(cfg, project, resume=False, path=None, fresh=False):
            seen.append((self.ptr.exists(), resume, fresh))
            return (len(seen) == 3), ("ok" if len(seen) == 3 else self.SERVED)
        with patch("claude_pm.spawn_session", side_effect=spawn):
            ok, msg = await claude_pm.refresh_session(self.CFG, "myproj")
        self.assertTrue(ok)
        self.kill.assert_called_once_with("cp-myproj")
        self.assertEqual(seen, [(False, True, True)] * 3)
        self.assertFalse(self.ptr.exists())
        self.assertIn("session_old", (self.tdir / "bridge-pointer.prev.json").read_text())

    async def test_failure_restores_pointer(self):
        async def spawn(*a, **k):
            return False, self.SERVED
        with patch("claude_pm.spawn_session", side_effect=spawn), \
             patch("claude_pm.REFRESH_WAIT_SEC", 0):
            ok, msg = await claude_pm.refresh_session(self.CFG, "myproj")
        self.assertFalse(ok)
        self.assertIn("session_old", self.ptr.read_text())
        self.assertIn("/refresh myproj", msg)

    async def test_other_error_does_not_retry(self):
        calls = []
        async def spawn(*a, **k):
            calls.append(1)
            return False, "Failed to start tmux session: boom"
        with patch("claude_pm.spawn_session", side_effect=spawn):
            ok, _ = await claude_pm.refresh_session(self.CFG, "myproj")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)

    def test_revive_leaves_pointer_alone(self):
        import inspect
        self.assertNotIn("BRIDGE_POINTER", inspect.getsource(claude_pm.revive_session))


class TestSessionDir(unittest.TestCase):
    def test_bot_session_resolves_to_repo(self):
        """/revive claude-pm used to fail: it only looked under projects_root."""
        d = claude_pm.session_dir({"projects_root": "/nonexistent", "tmux_prefix": "cp"},
                                  "claude-pm")
        self.assertEqual(d, Path(claude_pm.__file__).resolve().parent)

    @patch("claude_pm.subprocess.run")
    def test_unknown_and_dead_is_none(self, run):
        run.return_value = MagicMock(returncode=1, stdout="")
        self.assertIsNone(claude_pm.session_dir(
            {"projects_root": "/nonexistent", "tmux_prefix": "cp"}, "ghost"))


class TestTmuxServerScope(unittest.TestCase):
    @patch("claude_pm.subprocess.run")
    def test_already_adopted_is_noop(self, run):
        run.return_value = MagicMock(returncode=0, stdout="4242\n")
        with patch("claude_pm.Path.read_text",
                   return_value="0::/user.slice/x/cpm-tmux-server.scope\n"):
            self.assertTrue(claude_pm.ensure_tmux_server_scope())
        self.assertEqual(run.call_count, 1)   # no busctl call

    def test_scope_name_outside_reaper_glob(self):
        """The reaper sweeps claude-*.scope; the tmux server's scope must never
        match, or reaping it would kill every session at once."""
        self.assertFalse(claude_pm.TMUX_SCOPE.startswith("claude-"))


class TestUsageCommand(unittest.TestCase):
    def test_simple_two_bars(self):
        now = claude_pm.datetime.now(claude_pm.timezone.utc)
        live = {"now": now, "pct5": 25.0, "pctw": 66.0, "status": "allowed",
                "reset5": now + claude_pm.timedelta(hours=4),
                "resetw": now + claude_pm.timedelta(days=2)}
        h = claude_pm.CommandHandler({"projects_root": "/tmp"}, logging.getLogger("t"))
        with patch("claude_pm.probe_rate_limits", return_value=live), \
             patch("claude_pm.claude_plan_info", return_value={"plan": "max"}):
            out = h._usage()
        self.assertIn("25%", out)
        self.assertIn("66%", out)
        self.assertLessEqual(len(out.splitlines()), 4)

    def test_probe_down(self):
        h = claude_pm.CommandHandler({"projects_root": "/tmp"}, logging.getLogger("t"))
        with patch("claude_pm.probe_rate_limits", return_value={}):
            self.assertIn("Couldn't read usage", h._usage())


def _load_gate():
    import importlib.machinery, importlib.util
    path = str(Path(claude_pm.__file__).parent / "templates" / "cpm-sudo")
    loader = importlib.machinery.SourceFileLoader("cpm_sudo_gate", path)
    spec = importlib.util.spec_from_loader("cpm_sudo_gate", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestSudoGate(unittest.TestCase):
    """templates/cpm-sudo: this session keeps root; others get approved scripts."""
    P = "/home/test/ai-projects"
    POLICY = {"privileged_scopes": ["claude-cp-claude-pm.scope"],
              "projects_root": "/home/test/ai-projects"}

    @classmethod
    def setUpClass(cls):
        cls.g = _load_gate()

    def decide(self, scope, args, cwd=None, ancestors=(), approvals=None, hashes=None):
        hashes = hashes or {}
        return self.g.decide(scope, list(args), cwd or f"{self.P}/webapp",
                             list(ancestors), self.POLICY, approvals or {},
                             lambda p: hashes.get(p))

    def test_privileged_session_keeps_full_root(self):
        self.assertEqual(self.decide("claude-cp-claude-pm.scope", ["-n", "dnf", "install", "x"]),
                         ("sudo", ["-n", "dnf", "install", "x"]))

    def test_other_session_denied_plain_command(self):
        action, why = self.decide("claude-cp-webapp.scope", ["dnf", "install", "x"])
        self.assertEqual(action, "deny")
        self.assertIn("/sudo approve webapp", why)

    def test_approved_script_runs(self):
        s = f"{self.P}/webapp/tools/setup.sh"
        out = self.decide("claude-cp-webapp.scope", ["-n", "./tools/setup.sh", "--fast"],
                          approvals={"webapp": {s: "abc"}}, hashes={s: "abc"})
        self.assertEqual(out, ("run", ["./tools/setup.sh", "--fast"]))

    def test_interpreter_form_runs(self):
        s = f"{self.P}/webapp/tools/setup.sh"
        out = self.decide("claude-cp-webapp.scope", ["bash", "tools/setup.sh"],
                          approvals={"webapp": {s: "abc"}}, hashes={s: "abc"})
        self.assertEqual(out[0], "run")

    def test_edited_script_loses_approval(self):
        s = f"{self.P}/webapp/tools/setup.sh"
        action, why = self.decide("claude-cp-webapp.scope", ["./tools/setup.sh"],
                                  approvals={"webapp": {s: "abc"}}, hashes={s: "EDITED"})
        self.assertEqual(action, "deny")
        self.assertIn("changed since it was approved", why)

    def test_other_projects_approval_does_not_count(self):
        s = f"{self.P}/webapp/tools/setup.sh"
        action, _ = self.decide("claude-cp-ctf.scope", [s],
                                approvals={"webapp": {s: "abc"}}, hashes={s: "abc"})
        self.assertEqual(action, "deny")

    def test_sudo_inside_approved_launch_sh(self):
        """launch.sh's `$SUDO dnf install …`, run by /run as a job."""
        s = f"{self.P}/myapp/launch.sh"
        out = self.decide("claude-job-myapp.scope", ["dnf", "install", "-y", "ghidra"],
                          ancestors=[(["bash", "launch.sh"], f"{self.P}/myapp")],
                          approvals={"myapp": {s: "h"}}, hashes={s: "h"})
        self.assertEqual(out, ("run", ["dnf", "install", "-y", "ghidra"]))

    def test_sudo_inside_unapproved_script_denied(self):
        out = self.decide("claude-job-myapp.scope", ["dnf", "install", "x"],
                          ancestors=[(["bash", "launch.sh"], f"{self.P}/myapp")])
        self.assertEqual(out[0], "deny")

    def test_user_switch_and_shell_flags_denied(self):
        for flags in (["-u", "root", "x"], ["-s"], ["-i"], ["-e", "/etc/x"]):
            self.assertEqual(self.decide("claude-cp-webapp.scope", flags)[0], "deny", flags)

    def test_only_bot_may_approve(self):
        self.assertEqual(self.decide("claude-pm.service", ["--approve", "p", "/x"])[0], "approve")
        self.assertEqual(self.decide("claude-cp-webapp.scope", ["--approve", "p", "/x"])[0], "deny")

    def test_non_session_caller_denied(self):
        self.assertEqual(self.decide("session-3.scope", ["ls"])[0], "deny")

    def test_scope_project_parsing(self):
        self.assertEqual(self.g.scope_project("claude-cp-webapp.scope"), ("cp", "webapp"))
        self.assertEqual(self.g.scope_project("claude-job-x-y.scope"), ("job", "x-y"))
        self.assertEqual(self.g.scope_project("claude-pm.service"), (None, None))


class TestSudoInstallConsistency(unittest.TestCase):
    ROOT = Path(claude_pm.__file__).parent

    def test_installer_sudoers_matches_code(self):
        text = (self.ROOT / "install-sudo-gate.sh").read_text()
        block = text.split("<<'SUDOERS'\n", 1)[1].split("\nSUDOERS\n", 1)[0] + "\n"
        self.assertEqual(block, claude_pm.SUDOERS_RULES_TEMPLATE)
        self.assertNotIn("NOPASSWD: ALL", block)

    def test_installer_keeps_this_session_privileged(self):
        text = (self.ROOT / "install-sudo-gate.sh").read_text()
        self.assertIn('"privileged_scopes": ["claude-cp-claude-pm.scope"]', text)

    def test_shim_is_plain_sudo_until_gate_installed(self):
        shim = (self.ROOT / "templates" / "sudo-shim").read_text()
        self.assertIn("if [ -x /usr/local/sbin/cpm-sudo ]", shim)
        self.assertTrue(shim.rstrip().endswith('exec /usr/bin/sudo "$@"'))

    def test_sessions_get_shim_on_path(self):
        self.assertIn(str(claude_pm.CLAUDE_BIN), claude_pm.SESSION_PATH_EXPORT)
        import inspect
        self.assertIn("SESSION_PATH_EXPORT", inspect.getsource(claude_pm.spawn_session))


class TestSudoCommand(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.d)
        (self.d / "proj" / "tools").mkdir(parents=True)
        (self.d / "proj" / "tools" / "setup.sh").write_text("#!/bin/bash\ndnf install -y x\n")
        self.h = claude_pm.CommandHandler({"projects_root": str(self.d), "tmux_prefix": "cp"},
                                          logging.getLogger("t"))

    def test_gate_missing_explains_install(self):
        with patch.object(claude_pm, "SUDO_GATE", self.d / "nope"):
            self.assertIn("install-sudo-gate.sh", self.h._sudo("list"))

    def test_approve_first_shows_script_and_hash(self):
        with patch.object(claude_pm, "SUDO_GATE", self.d / "proj" / "tools" / "setup.sh"), \
             patch("claude_pm.subprocess.run") as run:
            out = self.h._sudo("approve", "proj", "tools/setup.sh")
        self.assertIn("Review before approving", out)
        self.assertIn("dnf install -y x", out)
        self.assertIn("confirm:", out)
        run.assert_not_called()   # nothing stored without the hash confirmation

    def test_path_outside_project_refused(self):
        with patch.object(claude_pm, "SUDO_GATE", self.d / "proj" / "tools" / "setup.sh"):
            self.assertIn("isn't a file inside", self.h._sudo("approve", "proj", "../../etc/passwd"))


class TestMemorySizes(unittest.TestCase):
    def test_units(self):
        self.assertEqual(claude_pm.parse_size("12G"), 12 * 1024 ** 3)
        self.assertEqual(claude_pm.parse_size("12 GB"), 12 * 1024 ** 3)
        self.assertEqual(claude_pm.parse_size("1536M"), 1536 * 1024 ** 2)
        self.assertEqual(claude_pm.parse_size("1.5G"), int(1.5 * 1024 ** 3))

    def test_bare_number_means_gigabytes(self):
        # Someone asking for "12" means 12 GB, not 12 bytes.
        self.assertEqual(claude_pm.parse_size("12"), 12 * 1024 ** 3)

    def test_junk_is_none(self):
        for t in ("", "lots", "12 parsecs", None):
            self.assertIsNone(claude_pm.parse_size(t), t)

    def test_fmt_round_trips_what_systemd_takes(self):
        for t in ("10G", "1536M", "2G"):
            self.assertEqual(claude_pm.fmt_size(claude_pm.parse_size(t)), t)


class TestMemGrants(unittest.TestCase):
    """The cap is a soft block: a grant has to outlive the session it was
    given to, or a /revive would silently put the job back under the old cap."""

    def setUp(self):
        self.cfg = {"session_mem_high": "6G", "session_mem_max": "10G",
                    "session_mem_swap_max": "2G", "tmux_prefix": "cp"}
        claude_pm._write_json(claude_pm.MEMGRANT_FILE, {})
        self.addCleanup(claude_pm._write_json, claude_pm.MEMGRANT_FILE, {})

    def test_defaults_without_a_grant(self):
        self.assertEqual(claude_pm.mem_caps_for(self.cfg, "proj"), ("6G", "10G", "2G"))

    def test_grant_overrides_the_default(self):
        claude_pm.set_mem_grant("proj", "10G", "12G", "3G", "ghidra")
        self.assertEqual(claude_pm.mem_caps_for(self.cfg, "proj"), ("10G", "12G", "3G"))
        self.assertEqual(claude_pm.mem_caps_for(self.cfg, "other"), ("6G", "10G", "2G"))

    def test_clearing_a_grant_restores_the_default(self):
        claude_pm.set_mem_grant("proj", "10G", "12G", "3G")
        claude_pm.set_mem_grant("proj", "", "", "")
        self.assertEqual(claude_pm.mem_caps_for(self.cfg, "proj"), ("6G", "10G", "2G"))

    def test_session_name_maps_back_to_the_project(self):
        self.assertEqual(claude_pm._sname_project(self.cfg, "cp-myproj"), "myproj")
        self.assertEqual(claude_pm._sname_project(self.cfg, "job-myproj"), "myproj")

    @patch("claude_pm.shutil.which", return_value="/usr/bin/systemd-run")
    @patch("claude_pm._free_scope_unit", return_value=True)
    @patch("claude_pm.ensure_sessions_slice", return_value=False)
    def test_new_session_starts_under_its_grant(self, *_):
        claude_pm.set_mem_grant("myproj", "10G", "12G", "3G")
        argv = claude_pm._mem_scope_prefix(self.cfg, "cp-myproj")
        self.assertIn("MemoryHigh=10G", argv)
        self.assertIn("MemoryMax=12G", argv)
        self.assertIn("MemorySwapMax=3G", argv)

    @patch("claude_pm._sctl")
    def test_live_caps_use_runtime_properties(self, sctl):
        sctl.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ok, _ = claude_pm.apply_mem_caps_live("claude-cp-myproj.scope", "10G", "12G", "3G")
        self.assertTrue(ok)
        args = sctl.call_args[0]
        self.assertEqual(args[:3], ("set-property", "--runtime", "claude-cp-myproj.scope"))
        self.assertIn("MemoryMax=12G", args)


class TestMemCommand(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.d)
        (self.d / "myproj").mkdir()
        self.cfg = {"projects_root": str(self.d), "tmux_prefix": "cp",
                    "session_mem_high": "6G", "session_mem_max": "10G",
                    "session_mem_swap_max": "2G", "mem_grant_ceiling": "13G",
                    "sessions_total_mem_high": "10G", "sessions_total_mem_max": "12G",
                    "sessions_total_swap_max": "3G"}
        self.h = claude_pm.CommandHandler(self.cfg, logging.getLogger("t"))
        claude_pm._write_json(claude_pm.MEMGRANT_FILE, {})
        claude_pm._write_json(claude_pm.MEMREQ_FILE, {})
        for f in (claude_pm.MEMGRANT_FILE, claude_pm.MEMREQ_FILE):
            self.addCleanup(claude_pm._write_json, f, {})

    def _grant(self, **kw):
        with patch.object(claude_pm.CommandHandler, "_mem_units", return_value=[]), \
             patch("claude_pm.save_config"), patch("claude_pm.ensure_sessions_slice"), \
             patch("claude_pm.apply_mem_caps_live", return_value=(True, "")):
            return self.h._mem(**kw)

    def test_grant_records_and_answers_the_request(self):
        claude_pm._write_json(claude_pm.MEMREQ_FILE, {
            "myproj": {"ask_max": "12G", "reason": "ghidra", "status": "pending"}})
        out = self._grant(action="grant", project="myproj", size="12G")
        self.assertIn("12G", out)
        self.assertEqual(claude_pm.read_mem_grants()["myproj"]["max"], "12G")
        self.assertEqual(claude_pm.read_mem_requests()["myproj"]["status"], "granted")

    def test_grant_defaults_the_soft_cap_below_the_hard_one(self):
        self._grant(action="grant", project="myproj", size="12G")
        g = claude_pm.read_mem_grants()["myproj"]
        self.assertEqual((g["high"], g["max"]), ("8G", "12G"))

    def test_grant_never_lowers_the_soft_cap(self):
        # 9G hard minus the default 4G head-room would be 5G — below the 6G
        # default soft cap, i.e. a "grant" that throttles the session sooner.
        self._grant(action="grant", project="myproj", size="9G")
        self.assertEqual(claude_pm.read_mem_grants()["myproj"]["high"], "6G")

    def test_grant_smaller_than_the_soft_cap_is_refused(self):
        out = self._grant(action="grant", project="myproj", size="5G")
        self.assertIn("has to be below the hard cap", out)
        self.assertNotIn("myproj", claude_pm.read_mem_grants())

    def test_grant_takes_the_size_from_the_request_when_omitted(self):
        claude_pm._write_json(claude_pm.MEMREQ_FILE, {
            "myproj": {"ask_max": "11G", "reason": "ghidra", "status": "pending"}})
        self._grant(action="grant", project="myproj")
        self.assertEqual(claude_pm.read_mem_grants()["myproj"]["max"], "11G")

    def test_grant_above_the_ceiling_is_refused_with_the_way_to_raise_it(self):
        out = self._grant(action="grant", project="myproj", size="14G")
        self.assertIn("ceiling", out)
        self.assertIn("action:ceiling", out)
        self.assertNotIn("myproj", claude_pm.read_mem_grants())

    def test_grant_raises_the_shared_slice_so_it_is_not_capped_there(self):
        self._grant(action="grant", project="myproj", size="13G")
        self.assertEqual(self.cfg["sessions_total_mem_max"], "13G")

    def test_deny_closes_the_request_and_keeps_the_cap(self):
        claude_pm._write_json(claude_pm.MEMREQ_FILE, {
            "myproj": {"ask_max": "12G", "reason": "ghidra", "status": "pending"}})
        out = self.h._mem("deny", "myproj")
        self.assertIn("Denied", out)
        self.assertEqual(claude_pm.read_mem_requests()["myproj"]["status"], "denied")
        self.assertNotIn("myproj", claude_pm.read_mem_grants())

    def test_revert_drops_the_grant(self):
        self._grant(action="grant", project="myproj", size="12G")
        with patch.object(claude_pm.CommandHandler, "_mem_units", return_value=[]):
            out = self.h._mem("revert", "myproj")
        self.assertIn("default", out)
        self.assertNotIn("myproj", claude_pm.read_mem_grants())

    def test_ceiling_is_bounded_by_the_laptop(self):
        with patch("claude_pm.save_config"):
            self.assertIn("more than the laptop has", self.h._mem("ceiling", size="20G"))
            self.assertIn("14G", self.h._mem("ceiling", size="14G"))


class TestProjectScaffolding(unittest.TestCase):
    """Every project gets script scaffolding and the sudo path — that used to
    be two of the wizard's nine questions."""

    def _write(self, **data):
        root = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root)
        data.setdefault("name", "proj")
        data.setdefault("description", "a thing")
        (root / data["name"]).mkdir()
        claude_pm.write_project_files({"projects_root": str(root)}, data)
        return root / data["name"]

    def test_every_project_gets_tools_setup_sh(self):
        for key in ("1", "4", "5", "8"):
            p = self._write(stack_key=key, name=f"p{key}")
            setup = p / "tools" / "setup.sh"
            self.assertTrue(setup.is_file(), key)
            self.assertTrue(os.access(setup, os.X_OK), key)
            self.assertIn("/sudo action:approve", setup.read_text())

    def test_claude_md_points_at_the_sudo_path(self):
        p = self._write(stack_key="1", stack_detail="fastapi")
        self.assertIn("tools/setup.sh", (p / "CLAUDE.md").read_text())

    def test_launch_script_follows_the_type_without_being_asked(self):
        web = self._write(stack_key="1", stack_detail="fastapi", name="w", port=8101)
        self.assertTrue((web / "launch.sh").is_file())
        self.assertIn("uvicorn", (web / "launch.sh").read_text())
        self.assertIn("8101", (web / "launch.sh").read_text())
        ctf = self._write(stack_key="5", stack_detail="crypto", name="c")
        self.assertFalse((ctf / "launch.sh").exists())

    def test_launch_md_explains_sudo_instead_of_enabling_it(self):
        web = self._write(stack_key="1", stack_detail="fastapi", name="w")
        text = (web / "LAUNCH.md").read_text()
        self.assertIn("tools/setup.sh", text)
        self.assertIn("SUDO=", (web / "launch.sh").read_text())
        # launch.sh stays unprivileged unless someone deliberately changes it
        self.assertIn("\nSUDO=\n", (web / "launch.sh").read_text())

    def test_condensed_web_row_still_writes_the_frameworks_project(self):
        p = self._write(stack_key="1", stack_detail="django", name="d")
        self.assertIn("Django", (p / "CLAUDE.md").read_text())
        self.assertIn("pytest", (p / "RULES.md").read_text())

    def test_re_project_gets_the_harness_playbook_and_parsers(self):
        p = self._write(stack_key="4", stack_detail="iOS ipa, armv7", name="r")
        skills = (p / "SKILLS.md").read_text()
        self.assertIn("REA", skills)
        self.assertIn("differential", skills.lower())
        self.assertTrue((p / "tools" / "macho.py").is_file())
        self.assertTrue((p / "target").is_dir())
        self.assertIn("sha256", (p / "CLAUDE.md").read_text()
                      + (p / "RULES.md").read_text())

    def test_ctf_authoring_project_is_not_the_solving_one(self):
        p = self._write(stack_key="6", stack_detail="web, medium", name="a")
        skills = (p / "SKILLS.md").read_text()
        self.assertIn("intended", skills.lower())
        self.assertTrue((p / "solve").is_dir())
        self.assertFalse((p / "assets").exists())   # that's the solving layout


class TestReaRegistration(unittest.TestCase):
    def test_rea_server_is_stdio_with_provider_env(self):
        srv = claude_pm.rea_mcp_server()
        self.assertEqual(srv["type"], "stdio")
        self.assertIn("mcp", srv["args"])
        self.assertTrue(srv["command"].endswith("rea") or srv["command"] == "npx")

    def test_android_tools_are_registered_only_when_they_exist(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        jar = d / "jadx.jar"
        with patch.object(claude_pm, "JADX_MCP_JAR", jar):
            self.assertNotIn("REA_JADX_MCP_JAR", claude_pm.rea_mcp_server()["env"])
            jar.write_bytes(b"x")
            env = claude_pm.rea_mcp_server()["env"]
        self.assertEqual(env["REA_JADX_MCP_JAR"], str(jar))
        # the heap is bounded under the session's soft cap, not left to the JVM
        self.assertEqual(env["REA_JADX_HEAP_MIB"], "3072")

    def test_mcp_servers_land_in_the_projects_local_scope(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        cj = d / ".claude.json"
        cj.write_text(json.dumps({"projects": {"/x": {"hasTrustDialogAccepted": True}}}))
        with patch.object(claude_pm, "CLAUDE_JSON", cj):
            claude_pm.trust_workspace("/p", None, mcp_servers={"rea": {"type": "stdio"}})
        data = json.loads(cj.read_text())
        self.assertEqual(data["projects"]["/p"]["mcpServers"]["rea"]["type"], "stdio")
        self.assertTrue(data["projects"]["/p"]["hasTrustDialogAccepted"])
        self.assertEqual(data["projects"]["/x"], {"hasTrustDialogAccepted": True})

    def test_trust_without_mcp_is_unchanged(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        cj = d / ".claude.json"
        with patch.object(claude_pm, "CLAUDE_JSON", cj):
            claude_pm.trust_workspace("/p")
        self.assertNotIn("mcpServers", json.loads(cj.read_text())["projects"]["/p"])


if __name__ == "__main__":
    unittest.main()


class TestReviveReattach(unittest.IsolatedAsyncioTestCase):
    """/revive reattaches by ID and says honestly whether it did. Before
    2026-10-26 it relied on remote-control's own reattach, which lapses after
    ~4 h, and always claimed "same conversation" (batterycover, 30 h idle)."""
    CFG = {"projects_root": "/x", "tmux_prefix": "cp", "revive_summary": False}

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        self.proj, self.tdir = d / "proj", d / "transcripts"
        self.proj.mkdir(); self.tdir.mkdir()
        self.ptr = self.tdir / "bridge-pointer.json"
        for target, kw in [("session_dir", {"return_value": self.proj}),
                           ("_transcript_dir", {"return_value": self.tdir}),
                           ("session_exists", {"return_value": False}),
                           ("trust_workspace", {})]:
            m = patch(f"claude_pm.{target}", **kw)
            m.start(); self.addCleanup(m.stop)

    def spawner(self, results, new_id=None):
        """Fake spawn_session: pops (ok, msg) per call, records session_id, and
        rewrites the pointer to `new_id` on success when given."""
        self.calls = []
        async def spawn(cfg, project, resume=False, path=None, fresh=False,
                        session_id=""):
            self.calls.append(session_id)
            ok, msg = results.pop(0)
            if ok and new_id:
                self.ptr.write_text(json.dumps({"sessionId": new_id}))
            return ok, msg
        return patch("claude_pm.spawn_session", side_effect=spawn)

    async def test_same_conversation_by_id(self):
        self.ptr.write_text('{"sessionId":"session_old"}')
        with self.spawner([(True, "up")]):
            ok, msg = await claude_pm.revive_session(self.CFG, "myproj")
        self.assertTrue(ok)
        self.assertEqual(self.calls, ["session_old"])
        self.assertIn("Same conversation", msg)

    async def test_failed_reattach_falls_back_and_says_new(self):
        self.ptr.write_text('{"sessionId":"session_old"}')
        with self.spawner([(False, "Session died unexpectedly."), (True, "up")],
                          new_id="session_new"):
            ok, msg = await claude_pm.revive_session(self.CFG, "myproj")
        self.assertTrue(ok)
        self.assertEqual(self.calls, ["session_old", ""])
        self.assertIn("New conversation", msg)
        self.assertNotIn("Same conversation", msg)

    async def test_pointer_changed_means_new(self):
        self.ptr.write_text('{"sessionId":"session_old"}')
        with self.spawner([(True, "up")], new_id="session_other"):
            _, msg = await claude_pm.revive_session(self.CFG, "myproj")
        self.assertIn("New conversation", msg)

    async def test_no_pointer_spawns_plain(self):
        with self.spawner([(True, "up")], new_id="session_new"):
            ok, msg = await claude_pm.revive_session(self.CFG, "myproj")
        self.assertTrue(ok)
        self.assertEqual(self.calls, [""])
        self.assertIn("no earlier conversation", msg)

    def test_bad_pointer_is_ignored(self):
        self.ptr.write_text('{"sessionId":"x; rm -rf ~"}')
        self.assertEqual(claude_pm.read_bridge_session(self.proj), "")
        self.ptr.write_text("not json")
        self.assertEqual(claude_pm.read_bridge_session(self.proj), "")

    def test_spawn_passes_session_id_flag(self):
        import inspect
        self.assertIn("--session-id", inspect.getsource(claude_pm.spawn_session))


class TestShareQueue(unittest.TestCase):
    """claude-share requests are vetted by the bot: any same-user process can
    write the queue, so only files inside that project's folder get posted."""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        self.proj, self.queue = d / "proj", d / "queue"
        self.proj.mkdir(); self.queue.mkdir()
        (self.proj / "render.png").write_bytes(b"png")
        self.outside = d / "botFiles-elsewhere.txt"
        self.outside.write_text("secret")
        m = patch("claude_pm.session_dir", return_value=self.proj)
        m.start(); self.addCleanup(m.stop)

    def queue_req(self, files, project="proj", caption="hi"):
        (self.queue / f"{len(list(self.queue.iterdir()))}.json").write_text(
            json.dumps({"project": project, "files": [str(f) for f in files],
                        "caption": caption}))

    def take(self):
        return claude_pm.take_share_requests({"projects_root": "/x",
                                              "tmux_prefix": "cp"}, self.queue)

    def test_inside_file_posted_and_request_removed(self):
        self.queue_req([self.proj / "render.png"])
        [(project, files, caption, rejected)] = self.take()
        self.assertEqual((project, caption, rejected), ("proj", "hi", []))
        self.assertEqual(files, [(self.proj / "render.png").resolve()])
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_outside_and_symlink_escape_rejected(self):
        link = self.proj / "sneaky.txt"
        link.symlink_to(self.outside)
        self.queue_req([self.outside, link, self.proj / ".." / self.outside.name])
        [(_, files, _, rejected)] = self.take()
        self.assertEqual(files, [])
        self.assertEqual(len(rejected), 3)

    def test_secret_names_and_oversize_rejected(self):
        (self.proj / "botFiles.md").write_text("token")
        (self.proj / ".env").write_text("K=V")
        big = self.proj / "big.bin"
        with open(big, "wb") as f:
            f.truncate(claude_pm.SHARE_MAX_BYTES + 1)
        self.queue_req([self.proj / "botFiles.md", self.proj / ".env", big])
        [(_, files, _, rejected)] = self.take()
        self.assertEqual(files, [])
        self.assertEqual(len(rejected), 3)

    def test_bad_project_name_dropped(self):
        self.queue_req([self.proj / "render.png"], project="../claude-pm")
        self.assertEqual(self.take(), [])
        self.assertEqual(list(self.queue.iterdir()), [])

    def test_garbage_request_dropped(self):
        (self.queue / "x.json").write_text("{not json")
        self.assertEqual(self.take(), [])
        self.assertEqual(list(self.queue.iterdir()), [])


class TestClaudeShareScript(unittest.TestCase):
    """templates/claude-share writes a queue entry the bot can read."""

    def setUp(self):
        import importlib.machinery, importlib.util
        src = str(Path(claude_pm.TEMPLATE_DIR) / "claude-share")
        loader = importlib.machinery.SourceFileLoader("claude_share", src)
        spec = importlib.util.spec_from_loader("claude_share", loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        self.mod.QUEUE_DIR = self.d / "queue"
        (self.d / "a.png").write_bytes(b"x")

    def run_share(self, *argv, project="myproj"):
        with patch.object(self.mod, "session_project", return_value=project), \
             patch("sys.stdout", new=io.StringIO()):
            return self.mod.main(list(argv))

    def test_queues_request(self):
        self.assertEqual(self.run_share(str(self.d / "a.png"), "--caption", "v12"), 0)
        [q] = list(self.mod.QUEUE_DIR.glob("*.json"))
        req = json.loads(q.read_text())
        self.assertEqual((req["project"], req["caption"]), ("myproj", "v12"))
        self.assertEqual(req["files"], [str((self.d / "a.png").resolve())])

    def test_outside_a_session_refuses(self):
        self.assertEqual(self.run_share(str(self.d / "a.png"), project=""), 1)
        self.assertFalse(self.mod.QUEUE_DIR.exists())

    def test_missing_file_queues_nothing(self):
        self.assertEqual(self.run_share(str(self.d / "nope.png")), 1)
        self.assertFalse(self.mod.QUEUE_DIR.exists())

    def test_scope_parsing(self):
        self.assertEqual(self.mod.SCOPE_RE.match("claude-cp-claude-pm.scope").group(1),
                         "claude-pm")
        self.assertEqual(self.mod.SCOPE_RE.match("claude-job-myproj.scope").group(1),
                         "myproj")



class TestNewProjectCard(unittest.IsolatedAsyncioTestCase):
    """/new as a tap-through card (2026-10-29): name + type as slash options,
    then dropdowns, one pre-filled pop-up, Create/Cancel."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.h = claude_pm.CommandHandler({"projects_root": self.tmp, "tmux_prefix": "cp"},
                                          logging.getLogger("t"))
        self.key = {v[1]: k for k, v in claude_pm.STACKS.items()}

    def start(self, name="my-api", lang="Web", desc="does things"):
        text, wiz = self.h.new_form_start(42, name, self.key[lang], desc)
        self.assertIsNotNone(wiz, text)
        return text, wiz

    def test_card_shows_summary_without_typed_confirm(self):
        text, wiz = self.start()
        self.assertIn("my-api", text)
        self.assertIn("Python + FastAPI", text)          # default pick
        self.assertNotIn("Type **yes**", text)
        self.assertFalse(wiz.is_active())                 # typed messages aren't captured

    def test_bad_or_taken_name_refused_up_front(self):
        text, wiz = self.h.new_form_start(42, "!!!", "1")
        self.assertIsNone(wiz)
        Path(self.tmp, "taken").mkdir()
        text, wiz = self.h.new_form_start(42, "taken", "1")
        self.assertIsNone(wiz)
        self.assertIn("already exists", text)
        _, wiz = self.h.new_form_start(42, "claude-pm", "1")
        self.assertIsNone(wiz)

    def test_pick_drives_the_stack(self):
        _, wiz = self.start()
        wiz.data["np_pick"] = "node express"
        self.assertIn("JavaScript + Express", claude_pm.new_form_text(wiz))

    def test_type_change_resets_pick_and_reshapes_modal(self):
        _, wiz = self.start()
        self.h.new_form_set_type(wiz, self.key["OpenSCAD"])
        self.assertEqual(wiz.data["np_pick"], "PLA")
        ids = [c.custom_id for c in claude_pm.new_form_modal(42, wiz).children]
        self.assertEqual(ids, ["name", "description"])   # no goals/rules for 3D parts
        self.h.new_form_set_type(wiz, self.key["RE"])
        self.assertEqual(wiz.data["np_pick"], "")
        ids = [c.custom_id for c in claude_pm.new_form_modal(42, wiz).children]
        self.assertEqual(ids, ["name", "description", "note", "goals", "rules"])

    def test_modal_prefilled_and_applied(self):
        _, wiz = self.start(desc="first")
        m = claude_pm.new_form_modal(42, wiz)
        self.assertEqual(m.children[0].default, "my-api")
        self.assertEqual(m.children[1].default, "first")
        Path(self.tmp, "taken").mkdir()
        note = self.h.new_form_apply(wiz, {"name": "taken", "description": "second",
                                           "goals": "a\nb"})
        self.assertIn("already exists", note)
        self.assertEqual(wiz.data["name"], "my-api")
        self.assertEqual(wiz.data["description"], "second")
        self.assertEqual(wiz.data["goals"], "a\nb")
        self.assertEqual(self.h.new_form_apply(wiz, {"name": "Better Name"}), "")
        self.assertEqual(wiz.data["name"], "better-name")

    def test_gameboy_ctf_routes_to_gbdk(self):
        _, wiz = self.start(lang="CTFDEV")
        self.assertEqual(wiz.data["np_pick"], "gb")
        claude_pm.new_form_compose(wiz)
        self.assertEqual(wiz._lang(), "GBDK")
        self.assertTrue(wiz.data["stack_detail"].startswith("ctf"))
        # composing again (every redraw does) must not double the prefix
        claude_pm.new_form_compose(wiz)
        self.assertEqual(wiz.data["stack_detail"].count("ctf"), 1)

    def test_bare_type_note_is_the_custom_type(self):
        _, wiz = self.start(lang="")
        wiz.data["np_note"] = "home-automation glue"
        self.assertIn("home-automation glue", claude_pm.new_form_text(wiz))

    async def test_create_runs_the_same_execute(self):
        _, wiz = self.start()
        seen = {}

        async def fake_execute():
            seen.update(wiz.data)
            return "✅ **my-api** is ready!"
        wiz._execute = fake_execute
        out = await self.h.new_form_create(42)
        self.assertIn("ready", out)
        self.assertEqual(seen["stack_detail"], "python fastapi")
        self.assertIsNone(self.h.new_form(42))
        self.assertIn("expired", await self.h.new_form_create(42))

    async def test_create_refused_if_name_got_taken(self):
        self.start()
        Path(self.tmp, "my-api").mkdir()
        out = await self.h.new_form_create(42)
        self.assertIn("already exists", out)
        self.assertIsNotNone(self.h.new_form(42))         # card kept to rename

    def test_cancel(self):
        self.start()
        self.assertIn("not created", self.h.new_form_cancel(42))
        self.assertIsNone(self.h.new_form(42))

    def test_view_fits_and_routes(self):
        for lang in ("Web", "RE", "OpenSCAD", "CTFDEV", ""):
            _, wiz = self.start(lang=lang)
            v = claude_pm.new_form_view(42, wiz)
            rows = {}
            for c in v.children:
                rows.setdefault(c.row, []).append(c)
                self.assertEqual(claude_pm.np_parse(c.custom_id)[1], 42)
            self.assertLessEqual(max(rows), 4)
            self.assertTrue(all(len(cs) <= 5 for cs in rows.values()))
            type_sel = v.children[0]
            self.assertEqual([o.value for o in type_sel.options if o.default],
                             [wiz.data["np_key"]])
            self.assertLessEqual(len(claude_pm.new_form_text(wiz)), 2000)
        self.assertIsNone(claude_pm.np_parse("pm|a|x"))


class TestFeatureMapSkill(unittest.TestCase):
    """The OpenSCAD skill's feature_map.py draws ProtoMan's labelled sheet for
    any STL (2026-10-29). A plate with a hole, built without OpenSCAD."""

    def test_plate_with_hole(self):
        try:
            import trimesh
            from shapely.geometry import Point, box
        except ImportError:
            self.skipTest("needs trimesh + shapely")
        scripts = (Path(claude_pm.__file__).parent / "scaffolds" / "3d-modeling"
                   / "printable-parametric-parts" / "scripts")
        sys.path.insert(0, str(scripts))
        try:
            import feature_map
        finally:
            sys.path.remove(str(scripts))
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        plate = box(10, 20, 90, 60).difference(Point(30, 40).buffer(4, 64))
        mesh = trimesh.creation.extrude_polygon(plate, 3)
        mesh.export(d / "plate.stl")
        info = feature_map.feature_map(d / "plate.stl", d / "out")
        self.assertTrue((d / "out" / "feature_map.png").stat().st_size > 10_000)
        self.assertEqual(info["size_mm"], [80.0, 40.0, 3.0])
        self.assertEqual(info["origin_in_model"], [10.0, 20.0])
        kinds = [f["kind"] for f in info["features"]]
        self.assertEqual(kinds.count("edge"), 4)
        self.assertEqual(kinds.count("hole"), 1)
        hole = next(f for f in info["features"] if f["kind"] == "hole")
        self.assertIn("Ø8.0", hole["value"])
        self.assertIn("center (20.0, 20.0)", hole["value"])   # (30,40) - origin
