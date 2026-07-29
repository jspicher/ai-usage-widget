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
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"window": {"width": 512}, "language": "ru"}, f)
            with mock.patch.object(widget, "CONFIG_PATH", path):
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

    def test_save_is_atomic_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            widget.CONFIG_HEALTH = healthy_config_state()
            real_replace = os.replace
            with (
                mock.patch.object(widget, "CONFIG_PATH", path),
                mock.patch.object(
                    widget.os, "replace", wraps=real_replace) as replace,
            ):
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
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            log_path = os.path.join(folder, "widget-error.log")
            corrupt = b'{"window":'
            with open(path, "wb") as f:
                f.write(corrupt)
            with (
                mock.patch.object(widget, "CONFIG_PATH", path),
                mock.patch.object(widget, "ERROR_LOG_PATH", log_path),
            ):
                cfg = widget.load_config()
                self.assertEqual(widget.CONFIG_HEALTH["status"], "corrupt")
                self.assertFalse(widget.save_config(cfg))
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), corrupt)

    def test_explicit_recovery_backs_up_corrupt_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            log_path = os.path.join(folder, "widget-error.log")
            corrupt = b"{broken"
            with open(path, "wb") as f:
                f.write(corrupt)
            with (
                mock.patch.object(widget, "CONFIG_PATH", path),
                mock.patch.object(widget, "ERROR_LOG_PATH", log_path),
            ):
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
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            corrupt = b"{broken"
            with open(path, "wb") as f:
                f.write(corrupt)
            widget.CONFIG_HEALTH = {
                "status": "corrupt",
                "error": widget.error_info("config_corrupt"),
                "recovery_required": True,
                "backup_path": None,
            }
            with (
                mock.patch.object(widget, "CONFIG_PATH", path),
                mock.patch.object(widget.shutil, "copy2", side_effect=OSError("no backup")),
            ):
                self.assertFalse(
                    widget.save_config(widget.DEFAULT_CONFIG, allow_recovery=True))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), corrupt)
            self.assertEqual(widget.CONFIG_HEALTH["status"], "backup_failed")

    def test_unwritable_target_returns_false_and_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "missing", "config.json")
            log_path = os.path.join(folder, "widget-error.log")
            widget.CONFIG_HEALTH = healthy_config_state()
            with (
                mock.patch.object(widget, "CONFIG_PATH", path),
                mock.patch.object(widget, "ERROR_LOG_PATH", log_path),
            ):
                self.assertFalse(widget.save_config(widget.DEFAULT_CONFIG))
            self.assertEqual(widget.CONFIG_HEALTH["status"], "write_failed")
            with open(log_path, "r", encoding="utf-8") as f:
                self.assertIn("config write failed", f.read())

    def test_config_for_ui_redacts_key_without_mutating_cfg(self):
        widget.CFG = widget.normalize_config({
            "openrouter": {"api_key": "super-secret"},
        })
        safe = widget.config_for_ui()
        self.assertEqual(safe["openrouter"]["api_key"], widget.REDACTED)
        self.assertEqual(widget.CFG["openrouter"]["api_key"], "super-secret")

    def test_save_api_never_writes_redacted_placeholder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            widget.CFG = widget.normalize_config({
                "openrouter": {"api_key": "super-secret"},
            })
            widget.CONFIG_HEALTH = healthy_config_state()
            widget.STATE = widget.State()
            with mock.patch.object(widget, "CONFIG_PATH", path):
                result = widget.JsApi().save_config_api({
                    "language": "ru",
                    "openrouter": {"api_key": widget.REDACTED},
                })
            self.assertTrue(result["ok"])
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["openrouter"]["api_key"], "super-secret")
            self.assertEqual(result["config"]["openrouter"]["api_key"], widget.REDACTED)

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
        dollars = widget.make_window(
            "balance", "balance", used_usd=25, limit_usd=100)
        self.assertEqual(high["used_pct"], 100)
        self.assertEqual(high["remaining_pct"], 0)
        self.assertEqual(low["used_pct"], 0)
        self.assertEqual(dollars["remaining_pct"], 75)

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
