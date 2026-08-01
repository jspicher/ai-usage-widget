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
        self.assertTrue(cfg["display"]["daily_markers"])

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
            "display": {"daily_markers": "yes"},
            "opencode": {"usage_endpoint": "https://example.invalid"},
        })
        self.assertEqual(cfg["refresh_interval_sec"], 600)
        self.assertEqual(cfg["language"], "en")
        self.assertEqual(
            cfg["window"],
            {"x": -120, "y": 40, "width": 200, "height": 1200, "on_top": True},
        )
        self.assertEqual(
            cfg["reset_alert"]["pct_jump_threshold"], widget.PCT_JUMP_MIN)
        self.assertEqual(cfg["reset_alert"]["resets_at_advance_sec"], 3600)
        self.assertTrue(cfg["display"]["daily_markers"])
        self.assertNotIn("opencode", cfg)

    def test_pct_jump_threshold_never_clamps_below_the_firing_floor(self):
        """A hand-edited 0 threshold would pop the alert window on every poll.

        resetwatch reports a reset whenever remaining_pct rises by at least the
        threshold, so 0 -- or any negative value, which clamps -- fires on every
        poll where the balance merely failed to fall. The event id follows
        remaining_pct, so the dedup does not suppress the repeats and nothing in
        the UI turns the popup off.
        """
        self.assertGreaterEqual(widget.PCT_JUMP_MIN, 1.0)
        for raw in (0, -5, 0.25):
            cfg = widget.normalize_config({
                "reset_alert": {"pct_jump_threshold": raw},
            })
            self.assertEqual(
                cfg["reset_alert"]["pct_jump_threshold"],
                widget.PCT_JUMP_MIN,
                "threshold %r" % raw,
            )

    def test_daily_markers_can_be_disabled(self):
        cfg = widget.normalize_config({
            "display": {"daily_markers": False},
        })
        self.assertFalse(cfg["display"]["daily_markers"])

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
            # "***" is what a masked field would echo back. The section is not
            # UI-writable at all, so this must never reach the real key.
            result = widget.JsApi().save_config_api({
                "language": "ru",
                "openrouter": {"api_key": "***"},
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

    def test_non_dict_claude_body_reports_an_unrecognized_format(self):
        """An HTTP 200 carrying a JSON scalar must not crash the fetcher.

        A proxy or edge error page can answer with a bare string or array, which
        is valid JSON. Without the guard the card showed "Internal widget error"
        with no login button and no key list, blaming the widget for a broken
        response.
        """
        credentials = {"token": "t", "subscription": None, "expires_at": None}
        for body in ("unauthorized", []):
            with (
                mock.patch.object(
                    widget, "read_claude_credentials",
                    return_value=(credentials, None)),
                mock.patch.object(widget, "http_get_json", return_value=body),
            ):
                result = widget.fetch_claude()
            self.assertFalse(result["ok"], repr(body))
            self.assertEqual(
                result["error"],
                {"code": "api_format_unrecognized",
                 "params": {"service": "Claude"}},
                repr(body),
            )

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

    def test_http_error_names_the_endpoint_that_failed(self):
        """Claude tries two endpoints, so "HTTP 502" alone is not diagnosable.

        api.anthropic.com and claude.ai fail for different reasons and need
        different fixes; the card and widget-error.log have to say which one
        answered.
        """
        url = "https://claude.ai/api/oauth/usage"
        failure = widget.urllib.error.HTTPError(
            url, 502, "Bad Gateway", None, None)
        with mock.patch.object(widget, "http_get_json", side_effect=failure):
            data, error = widget.request_provider_json(
                url, {}, "Claude", "claude_auth_expired")
        self.assertIsNone(data)
        self.assertEqual(error["code"], "api_http_error")
        self.assertEqual(error["params"], {"service": "Claude", "status": 502})
        self.assertEqual(error["detail"], url)

    def test_auth_failures_still_route_to_the_relogin_code(self):
        """Adding the URL detail must not disturb the 401/403 branch."""
        failure = widget.urllib.error.HTTPError(
            "https://chatgpt.com/backend-api/wham/usage", 401, "no", None, None)
        with mock.patch.object(widget, "http_get_json", side_effect=failure):
            _data, error = widget.request_provider_json(
                "https://chatgpt.com/backend-api/wham/usage", {}, "Codex",
                "codex_auth_expired")
        self.assertEqual(
            error, {"code": "codex_auth_expired", "params": {"status": 401}})

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


class FakeAlertWindow:
    """Stand in for the pywebview alert window; records what was asked of it."""

    def __init__(self):
        self.sizes = []
        self.renders = 0
        self.destroyed = False

    def resize(self, width, height):
        self.sizes.append((width, height))

    def evaluate_js(self, script):
        self.renders += 1

    def destroy(self):
        self.destroyed = True


class TestAlertWindowSizing(unittest.TestCase):
    def test_height_follows_the_row_count_up_to_the_maximum(self):
        """The window is frameless, not resizable, and hides overflow."""
        manager = widget.AlertWindowManager
        self.assertEqual(manager.height_for(1), 220)
        self.assertEqual(manager.height_for(2), 350)
        self.assertEqual(manager.height_for(5), manager.MAX_HEIGHT)

    def test_second_alert_resizes_the_open_window_before_rerendering(self):
        """A second provider's reset must not render below the visible area.

        The window was sized for one alert; without the resize the new row's
        provider name, percentages, and Dismiss button are unreachable.
        """
        manager = widget.AlertWindowManager()
        window = FakeAlertWindow()
        manager.window = window
        alerts = mock.Mock()
        alerts.pending = [{"id": "a"}, {"id": "b"}]
        with mock.patch.object(widget, "ALERTS", alerts):
            manager.raise_alert()
        self.assertEqual(
            window.sizes,
            [(manager.WIDTH, widget.AlertWindowManager.height_for(2))],
        )
        self.assertEqual(window.renders, 1)

    def test_partial_dismissal_shrinks_the_window(self):
        """Dismissing two of three alerts must not leave a 460px window empty."""
        manager = widget.AlertWindowManager()
        window = FakeAlertWindow()
        manager.window = window
        alerts = mock.Mock()
        alerts.pending = [{"id": "a"}]
        with mock.patch.object(widget, "ALERTS", alerts):
            manager.refit_or_close()
        self.assertEqual(
            window.sizes, [(manager.WIDTH, manager.height_for(1))])
        self.assertFalse(window.destroyed)

    def test_last_dismissal_still_closes_the_window(self):
        manager = widget.AlertWindowManager()
        window = FakeAlertWindow()
        manager.window = window
        alerts = mock.Mock()
        alerts.pending = []
        with mock.patch.object(widget, "ALERTS", alerts):
            manager.refit_or_close()
        self.assertTrue(window.destroyed)
        self.assertIsNone(manager.window)


class TestTrayMenu(WidgetTestCase):
    @staticmethod
    def _labels(menu):
        return [item.text for item in menu
                if item is not widget.pystray.Menu.SEPARATOR]

    def test_menu_labels_follow_a_later_language_change(self):
        """Switching to Russian used to leave the tray menu in English.

        The window, toast, and tooltip all switched while right-clicking the
        tray still showed "Show / Refresh / Exit"; only a restart fixed it.
        """
        if not widget.TRAY_AVAILABLE:
            self.skipTest("pystray is unavailable")
        widget.CFG = widget.normalize_config({"language": "en"})
        tray = widget.TrayManager()
        with mock.patch.object(
                widget.pystray, "Icon", return_value=mock.Mock()) as ctor:
            tray.start(mock.Mock())
        menu = ctor.call_args.args[3]
        self.assertEqual(self._labels(menu), ["Show", "Refresh", "Exit"])
        # start() returns early once the icon exists and never reassigns the
        # menu, so these labels have to resolve against CFG when they are read.
        widget.CFG = widget.normalize_config({"language": "ru"})
        self.assertEqual(
            self._labels(menu), ["Показать", "Обновить", "Выход"])

    def test_language_change_rebuilds_the_cached_native_menu(self):
        """Windows caches the popup as an HMENU built once per update_menu()."""
        tray = widget.TrayManager()
        tray.icon = mock.Mock()
        tray.apply_language()
        tray.icon.update_menu.assert_called_once_with()


class FakeScreen:
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class FakeMainWindow:
    def __init__(self, x=120, y=240):
        self.x = x
        self.y = y
        self.width = 380
        self.height = 400


class TestWindowGeometry(WidgetTestCase):
    PRIMARY = FakeScreen(0, 0, 1920, 1080)

    def setUp(self):
        super().setUp()
        self.original_saved = widget._GEOMETRY_SAVED
        widget._GEOMETRY_SAVED = False

    def tearDown(self):
        widget._GEOMETRY_SAVED = self.original_saved
        super().tearDown()

    def test_position_on_a_missing_monitor_falls_back_to_the_default(self):
        """An undocked second monitor used to hide the window off-desktop.

        The tray icon appeared but Show did nothing visible, and the frameless
        window's pin, settings, and close buttons were unreachable.
        """
        self.assertEqual(
            widget.visible_position(2600, 300, 380, 400, [self.PRIMARY]),
            (None, None),
        )
        # A sliver at the edge is not "visible" either: the controls sit in the
        # part that is still off-screen.
        self.assertEqual(
            widget.visible_position(1900, 300, 380, 400, [self.PRIMARY]),
            (None, None),
        )

    def test_position_on_a_present_monitor_is_kept(self):
        second = FakeScreen(1920, 0, 1920, 1080)
        self.assertEqual(
            widget.visible_position(120, 240, 380, 400, [self.PRIMARY]),
            (120, 240),
        )
        self.assertEqual(
            widget.visible_position(2600, 300, 380, 400, [self.PRIMARY, second]),
            (2600, 300),
        )

    def test_unreadable_screen_layout_trusts_the_saved_position(self):
        """Never move a window that was probably fine just because a query
        failed; the caller passes None when screen enumeration raised."""
        self.assertEqual(
            widget.visible_position(2600, 300, 380, 400, None), (2600, 300))
        self.assertEqual(
            widget.visible_position(2600, 300, 380, 400, []), (2600, 300))
        self.assertEqual(
            widget.visible_position(None, None, 380, 400, [self.PRIMARY]),
            (None, None),
        )

    def test_closing_handler_saves_position_while_the_window_lives(self):
        """Alt+F4 and session logoff never run shutdown_app.

        The old fallback ran after webview.start() returned, when reading
        window.x raises, so a dragged widget reopened at its old corner forever.
        """
        with config_sandbox() as (_folder, path, _log):
            widget.CFG = widget.normalize_config({})
            widget.CONFIG_HEALTH = healthy_config_state()
            widget.save_geometry_on_close(FakeMainWindow(x=120, y=240))()
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        self.assertEqual(saved["window"]["x"], 120)
        self.assertEqual(saved["window"]["y"], 240)
        self.assertTrue(widget._GEOMETRY_SAVED)

    def test_closing_handler_never_reports_failure_to_pywebview(self):
        """pywebview cancels the close when a closing handler returns False, so
        a failed geometry save must not trap the user in an unclosable window."""
        widget.CFG = widget.normalize_config({})
        with mock.patch.object(widget, "commit_config", return_value=False):
            result = widget.save_geometry_on_close(FakeMainWindow())()
        self.assertIsNone(result)
        self.assertFalse(widget._GEOMETRY_SAVED)


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
