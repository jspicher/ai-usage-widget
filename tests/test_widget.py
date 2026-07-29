import contextlib
import copy
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import widget


def healthy_config_state():
    return {
        "status": "ok",
        "error": None,
        "recovery_required": False,
        "backup_path": None,
    }


@contextlib.contextmanager
def config_sandbox():
    """Point CONFIG_PATH and ERROR_LOG_PATH at a throwaway directory.

    Both patches matter. Without ERROR_LOG_PATH, any test that walks a failure
    path appends to the real widget-error.log sitting next to the source tree.
    """
    with tempfile.TemporaryDirectory() as folder:
        config_path = os.path.join(folder, "config.json")
        log_path = os.path.join(folder, "widget-error.log")
        with (
            mock.patch.object(widget, "CONFIG_PATH", config_path),
            mock.patch.object(widget, "ERROR_LOG_PATH", log_path),
        ):
            yield folder, config_path, log_path


class WidgetTestCase(unittest.TestCase):
    def setUp(self):
        self.original_cfg = widget.CFG
        self.original_health = widget.CONFIG_HEALTH
        self.original_state = widget.STATE

    def tearDown(self):
        widget.CFG = self.original_cfg
        widget.CONFIG_HEALTH = self.original_health
        widget.STATE = self.original_state


class TestConfig(WidgetTestCase):
    def test_load_merges_nested_values_and_preserves_defaults(self):
        with config_sandbox() as (_folder, path, _log):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"window": {"width": 512}, "language": "ru"}, f)
            cfg = widget.load_config()
        self.assertEqual(cfg["window"]["width"], 512)
        self.assertEqual(cfg["window"]["height"], 400)
        self.assertEqual(cfg["language"], "ru")
        self.assertEqual(cfg["refresh_interval_sec"], 300)

    def test_normalize_clamps_supported_values_and_drops_retired_config(self):
        cfg = widget.normalize_config({
            "refresh_interval_sec": "9999",
            "language": "de",
            "window": {
                "width": "100",
                "height": "5000",
                "x": "-120",
                "y": "40.9",
                "on_top": "yes",
            },
            "reset_alert": {
                "enabled": "yes",
                "pct_jump_threshold": -5,
                "resets_at_advance_sec": "invalid",
            },
            "opencode": {"usage_endpoint": "https://example.invalid"},
        })
        self.assertEqual(cfg["refresh_interval_sec"], 600)
        self.assertEqual(cfg["language"], "en")
        self.assertEqual(
            cfg["window"],
            {"x": -120, "y": 40, "width": 200, "height": 1200, "on_top": True},
        )
        self.assertEqual(cfg["reset_alert"]["pct_jump_threshold"], 0)
        self.assertEqual(cfg["reset_alert"]["resets_at_advance_sec"], 3600)
        self.assertNotIn("opencode", cfg)

    def test_invalid_hand_edited_values_use_documented_defaults(self):
        cfg = widget.normalize_config({
            "refresh_interval_sec": "five minutes",
            "window": {"width": [], "height": None, "x": "left", "y": {}},
        })
        self.assertEqual(cfg["refresh_interval_sec"], 300)
        self.assertEqual(cfg["window"]["width"], 380)
        self.assertEqual(cfg["window"]["height"], 400)
        self.assertIsNone(cfg["window"]["x"])
        self.assertIsNone(cfg["window"]["y"])

    def test_window_fallbacks_follow_default_config(self):
        defaults = copy.deepcopy(widget.DEFAULT_CONFIG)
        defaults["window"]["width"] = 444
        defaults["window"]["height"] = 555
        with mock.patch.object(widget, "DEFAULT_CONFIG", defaults):
            cfg = widget.normalize_config({"window": {}})
        self.assertEqual(cfg["window"]["width"], 444)
        self.assertEqual(cfg["window"]["height"], 555)

    def test_save_is_atomic_and_leaves_no_temporary_file(self):
        with config_sandbox() as (folder, path, _log):
            widget.CONFIG_HEALTH = healthy_config_state()
            real_replace = os.replace
            with mock.patch.object(
                    widget.os, "replace", wraps=real_replace) as replace:
                self.assertTrue(widget.save_config(widget.DEFAULT_CONFIG))
            replace.assert_called_once()
            source, target = replace.call_args.args
            self.assertEqual(target, path)
            self.assertNotEqual(source, target)
            self.assertEqual(
                [name for name in os.listdir(folder) if name.endswith(".tmp")],
                [],
            )

    def test_corrupt_file_is_untouched_by_automatic_save(self):
        with config_sandbox() as (_folder, path, _log):
            corrupt = b'{"window":'
            with open(path, "wb") as f:
                f.write(corrupt)
            cfg = widget.load_config()
            self.assertEqual(widget.CONFIG_HEALTH["status"], "corrupt")
            self.assertFalse(widget.save_config(cfg))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), corrupt)

    def test_explicit_recovery_backs_up_corrupt_file(self):
        with config_sandbox() as (folder, path, _log):
            corrupt = b"{broken"
            with open(path, "wb") as f:
                f.write(corrupt)
            widget.load_config()
            self.assertTrue(
                widget.save_config(widget.DEFAULT_CONFIG, allow_recovery=True))
            backups = [
                name for name in os.listdir(folder)
                if name.startswith("config.json.corrupt-")
                and name.endswith(".bak")
            ]
            self.assertEqual(len(backups), 1)
            with open(os.path.join(folder, backups[0]), "rb") as f:
                self.assertEqual(f.read(), corrupt)
            with open(path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f)["refresh_interval_sec"], 300)
            self.assertEqual(widget.CONFIG_HEALTH["status"], "recovered")

    def test_recovery_aborts_when_backup_fails(self):
        with config_sandbox() as (_folder, path, _log):
            corrupt = b"{broken"
            with open(path, "wb") as f:
                f.write(corrupt)
            widget.CONFIG_HEALTH = {
                "status": "corrupt",
                "error": widget.error_info("config_corrupt"),
                "recovery_required": True,
                "backup_path": None,
            }
            with mock.patch.object(
                    widget.shutil, "copy2", side_effect=OSError("no backup")):
                self.assertFalse(
                    widget.save_config(widget.DEFAULT_CONFIG, allow_recovery=True))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), corrupt)
            self.assertEqual(widget.CONFIG_HEALTH["status"], "backup_failed")

    def test_unwritable_target_returns_false_and_logs(self):
        with config_sandbox() as (folder, _path, log_path):
            widget.CONFIG_HEALTH = healthy_config_state()
            missing = os.path.join(folder, "missing", "config.json")
            with mock.patch.object(widget, "CONFIG_PATH", missing):
                self.assertFalse(widget.save_config(widget.DEFAULT_CONFIG))
            self.assertEqual(widget.CONFIG_HEALTH["status"], "write_failed")
            with open(log_path, "r", encoding="utf-8") as f:
                self.assertIn("config write failed", f.read())

    def test_config_for_ui_omits_openrouter_without_mutating_cfg(self):
        widget.CFG = widget.normalize_config({
            "openrouter": {"api_key": "super-secret"},
        })
        safe = widget.config_for_ui()
        # The section is dropped, not masked: a mask still tells the page
        # whether a key exists.
        self.assertNotIn("openrouter", safe)
        self.assertNotIn("super-secret", json.dumps(safe))
        self.assertEqual(widget.CFG["openrouter"]["api_key"], "super-secret")

    def test_save_api_never_writes_redacted_placeholder(self):
        with config_sandbox() as (_folder, path, _log):
            widget.CFG = widget.normalize_config({
                "openrouter": {"api_key": "super-secret"},
            })
            widget.CONFIG_HEALTH = healthy_config_state()
            widget.STATE = widget.State()
            result = widget.JsApi().save_config_api({
                "language": "ru",
                "openrouter": {"api_key": widget.REDACTED},
            })
            self.assertTrue(result["ok"])
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["openrouter"]["api_key"], "super-secret")
            self.assertNotIn("openrouter", result["config"])

    def test_failed_save_does_not_update_cfg(self):
        widget.CFG = widget.normalize_config({"language": "en"})
        widget.CONFIG_HEALTH = healthy_config_state()
        with mock.patch.object(widget, "save_config", return_value=False):
            result = widget.JsApi().save_config_api({"language": "ru"})
        self.assertFalse(result["ok"])
        self.assertEqual(widget.CFG["language"], "en")


class TestNormalization(unittest.TestCase):
    def test_iso_to_epoch_supports_seconds_milliseconds_and_numeric_strings(self):
        self.assertEqual(widget.iso_to_epoch(1_790_000_000), 1_790_000_000)
        self.assertEqual(widget.iso_to_epoch(1_790_000_000_000), 1_790_000_000)
        self.assertEqual(widget.iso_to_epoch("1790000000000"), 1_790_000_000)
        self.assertEqual(
            widget.iso_to_epoch("2026-09-21T14:13:20Z"),
            1_790_000_000,
        )
        self.assertIsNone(widget.iso_to_epoch("not-a-date"))

    def test_make_window_clamps_and_derives_remaining(self):
        high = widget.make_window("week", "week", used_pct=130)
        low = widget.make_window("week", "week", used_pct=-4)
        blank = widget.make_window("week", "week")
        self.assertEqual(high["used_pct"], 100)
        self.assertEqual(high["remaining_pct"], 0)
        self.assertEqual(low["used_pct"], 0)
        self.assertEqual(low["remaining_pct"], 100)
        self.assertIsNone(blank["remaining_pct"])

    def test_tooltip_prefers_week_then_session_then_first(self):
        session = {"id": "session"}
        week = {"id": "week"}
        month = {"id": "month"}
        self.assertIs(
            widget.tooltip_window({"windows": [session, week]}), week)
        self.assertIs(
            widget.tooltip_window({"windows": [month, session]}), session)
        self.assertIs(
            widget.tooltip_window({"windows": [month]}), month)
        self.assertIsNone(widget.tooltip_window({"windows": []}))

    def test_poll_delay_handles_numeric_strings_and_malformed_values(self):
        self.assertEqual(widget.poll_delay({"refresh_interval_sec": "45"}), 45)
        self.assertEqual(
            widget.poll_delay({"refresh_interval_sec": "five minutes"}), 300)
        self.assertEqual(widget.poll_delay({"refresh_interval_sec": -1}), 15)


class TestCredentials(unittest.TestCase):
    def test_claude_credential_reader_tries_every_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = os.path.join(folder, "broken.json")
            empty = os.path.join(folder, "empty.json")
            usable = os.path.join(folder, "usable.json")
            with open(broken, "w", encoding="utf-8") as f:
                f.write("{")
            with open(empty, "w", encoding="utf-8") as f:
                json.dump({"claudeAiOauth": {}}, f)
            with open(usable, "w", encoding="utf-8") as f:
                json.dump({
                    "claudeAiOauth": {
                        "accessToken": "usable-token",
                        "expiresAt": 1_900_000_000_000,
                        "subscriptionType": "max",
                    },
                }, f)
            credentials, error = widget.read_claude_credentials(
                [broken, empty, usable])
        self.assertIsNone(error)
        self.assertEqual(credentials["token"], "usable-token")
        self.assertEqual(credentials["expires_at"], 1_900_000_000)
        self.assertEqual(credentials["subscription"], "max")

    def test_fetch_snapshot_contains_expiry_but_not_access_token(self):
        credentials = {
            "token": "do-not-leak",
            "subscription": "max",
            "expires_at": 1_900_000_000,
            "source": "credentials.json",
        }
        response = {
            "seven_day": {
                "utilization": 25,
                "resets_at": 1_900_000_000,
            },
        }
        with (
            mock.patch.object(
                widget, "read_claude_credentials",
                return_value=(credentials, None)),
            mock.patch.object(widget, "http_get_json", return_value=response),
        ):
            result = widget.fetch_claude()
        self.assertTrue(result["ok"])
        self.assertEqual(result["meta"]["token_expires_at"], 1_900_000_000)
        self.assertNotIn("do-not-leak", json.dumps(result))

    def test_token_status_uses_cached_expiry(self):
        providers = {
            "claude": {"meta": {"token_expires_at": 10_100}},
            "codex": {"meta": {"token_expires_at": 20_000}},
        }
        status = widget.token_status_from_snapshot(providers, current_time=10_000)
        self.assertEqual(status["claude"]["status"], "expiring")
        self.assertEqual(status["codex"]["status"], "valid")
        expired = widget.token_status_from_snapshot(
            {"claude": {"meta": {"token_expires_at": 9_000}}},
            current_time=10_000,
        )
        self.assertEqual(expired["claude"], {"status": "expired", "remaining": 0})


class TestPolling(WidgetTestCase):
    def test_wake_event_applies_interval_changes_immediately(self):
        widget.STATE = widget.State()
        widget.CFG = widget.normalize_config({"refresh_interval_sec": 600})
        refreshed = threading.Event()
        call_count = 0

        def fake_refresh():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                refreshed.set()

        with mock.patch.object(widget, "refresh_all", side_effect=fake_refresh):
            thread = threading.Thread(target=widget.refresh_loop, daemon=True)
            thread.start()
            deadline = time.time() + 1
            while call_count < 1 and time.time() < deadline:
                time.sleep(0.005)
            widget.CFG["refresh_interval_sec"] = 15
            widget.STATE.refresh_wake_event.set()
            self.assertTrue(refreshed.wait(1))
            widget.STATE.shutdown_event.set()
            widget.STATE.refresh_wake_event.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_internal_provider_error_omits_traceback_detail(self):
        widget.STATE = widget.State()
        ok_provider = {
            "id": "provider",
            "name": "provider",
            "ok": True,
            "windows": [],
            "meta": {},
            "error": None,
        }
        with (
            mock.patch.object(
                widget, "fetch_claude", side_effect=RuntimeError("boom")),
            mock.patch.object(widget, "fetch_codex", return_value=ok_provider),
            mock.patch.object(widget, "fetch_openrouter", return_value=ok_provider),
            mock.patch.object(widget, "process_reset_alerts"),
            mock.patch.object(widget, "_log_failure"),
            mock.patch.object(widget.TRAY, "update_tooltip"),
        ):
            widget.refresh_all()
        error = widget.STATE.snapshot["providers"]["claude"]["error"]
        self.assertEqual(error, {"code": "internal_error"})


class TestProviderPayloads(unittest.TestCase):
    def test_every_provider_result_carries_a_name_and_kind(self):
        for provider_id in widget.PROVIDER_INFO:
            result = widget.provider_result(provider_id)
            self.assertEqual(result["id"], provider_id)
            self.assertTrue(result["name"])
            self.assertIn(result["kind"], ("windows", "balance"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["windows"], [])

    def test_internal_failure_keeps_the_display_name_and_kind(self):
        """A crashed fetcher must not retitle the card from "Claude Code" to "claude"."""
        widget_state = widget.STATE
        widget.STATE = widget.State()
        try:
            with (
                mock.patch.object(
                    widget, "fetch_claude", side_effect=RuntimeError("boom")),
                mock.patch.object(
                    widget, "fetch_codex",
                    return_value=widget.provider_result("codex")),
                mock.patch.object(
                    widget, "fetch_openrouter",
                    return_value=widget.provider_result("openrouter")),
                mock.patch.object(widget, "process_reset_alerts"),
                mock.patch.object(widget, "_log_failure"),
                mock.patch.object(widget.TRAY, "update_tooltip"),
            ):
                widget.refresh_all()
            failed = widget.STATE.snapshot["providers"]["claude"]
        finally:
            widget.STATE = widget_state
        self.assertEqual(failed["name"], "Claude Code")
        self.assertEqual(failed["kind"], "windows")

    def test_resolve_resets_flags_only_derived_timestamps(self):
        absolute, derived = widget._resolve_resets({"resets_at": 1_790_000_000})
        self.assertEqual(absolute, 1_790_000_000)
        self.assertFalse(derived)
        # Both spellings of the relative form must be honoured; a value derived
        # from now() moves with the clock and has to be flagged for resetwatch.
        for key in ("resets_in_seconds", "reset_after_seconds"):
            value, flag = widget._resolve_resets({key: 60})
            self.assertTrue(flag, key)
            self.assertAlmostEqual(value, time.time() + 60, delta=5)
        self.assertEqual(widget._resolve_resets({}), (None, False))


class TestAlertLocalization(WidgetTestCase):
    def test_alert_api_exposes_current_supported_language(self):
        widget.CFG = widget.normalize_config({"language": "ru"})
        self.assertEqual(widget.AlertApi().get_language(), "ru")
        widget.CFG["language"] = "unsupported"
        self.assertEqual(
            widget.AlertApi().get_language(),
            widget.DEFAULT_CONFIG["language"],
        )

    def test_native_text_covers_every_key_in_both_languages(self):
        english = widget.NATIVE_TEXT["en"]
        for language, table in widget.NATIVE_TEXT.items():
            self.assertEqual(
                set(table), set(english), "%s table differs" % language)
        self.assertEqual(set(widget.NATIVE_TEXT), set(widget.SUPPORTED_LANGUAGES))

    def test_native_text_follows_the_configured_language(self):
        widget.CFG = widget.normalize_config({"language": "ru"})
        self.assertEqual(widget.native_text("tray_exit"), "Выход")
        self.assertIn("2", widget.native_text("toast_many", count=2))
        widget.CFG["language"] = "unsupported"
        self.assertEqual(widget.native_text("tray_exit"), "Exit")


class TestCli(unittest.TestCase):
    def test_exe_is_run_directly(self):
        with mock.patch.object(
            widget.shutil, "which", return_value=r"C:\Tools\claude.exe"
        ):
            command, error = widget.resolve_cli_command(
                "claude", ["auth", "login"])
        self.assertIsNone(error)
        self.assertEqual(
            command, [r"C:\Tools\claude.exe", "auth", "login"])

    def test_command_wrapper_runs_through_comspec(self):
        with (
            mock.patch.object(
                widget.shutil, "which", return_value=r"C:\Tools\codex.cmd"),
            mock.patch.dict(
                os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
        ):
            command, error = widget.resolve_cli_command("codex", ["login"])
        self.assertIsNone(error)
        self.assertEqual(command[:4], [
            r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"])
        self.assertEqual(command[4:], [r"C:\Tools\codex.cmd", "login"])

    def test_nonzero_early_exit_is_failure(self):
        proc = mock.Mock()
        proc.wait.return_value = 7
        with (
            mock.patch.object(
                widget.shutil, "which", return_value=r"C:\Tools\codex.exe"),
            mock.patch.object(widget.subprocess, "Popen", return_value=proc) as popen,
        ):
            result = widget.launch_cli_login("codex", ["login"])
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["error"]["code"], "cli_early_exit")
        kwargs = popen.call_args.kwargs
        self.assertNotIn("encoding", kwargs)
        self.assertNotIn("errors", kwargs)

    def test_timeout_means_interactive_login_is_running(self):
        proc = mock.Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("claude", 2)
        with (
            mock.patch.object(
                widget.shutil, "which", return_value=r"C:\Tools\claude.exe"),
            mock.patch.object(widget.subprocess, "Popen", return_value=proc),
        ):
            result = widget.launch_cli_login("claude", ["auth", "login"])
        self.assertTrue(result["success"])
        self.assertEqual(result["value"], "login_started")


if __name__ == "__main__":
    unittest.main()
