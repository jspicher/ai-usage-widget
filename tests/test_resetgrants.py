import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import resetgrants
import resetwatch
import widget


NOW = 2_000_000_000
EXPIRY = NOW + 36_000


def claude_grant(identity="grant-a", left=2, expires=EXPIRY, **extra):
    return {"id": identity, "resets_left": left, "resets_total": 2,
            "starts_at": NOW - 100, "ends_at": expires, "paused": False,
            "usable_now": True, "clears": ["seven_day"], **extra}


def credit(identity="credit-a", expires=EXPIRY, **extra):
    return {"id": identity, "reset_type": "codex_rate_limits", "status": "available",
            "granted_at": NOW - 100, "expires_at": expires, **extra}


def observation(rows=None, stamp=NOW, provider="claude", total=None):
    rows = [claude_grant()] if rows is None else rows
    block = (resetgrants.claude({"cedar_ember": {"eligible": True, "grants": rows}}, stamp)
             if provider == "claude" else resetgrants.codex(
                 {"available_count": len(rows) if total is None else total, "credits": rows}, stamp))
    return {provider: {"ok": True, "meta": {"banked_resets": block}, "windows": []}}


class TestGrantNormalization(unittest.TestCase):
    def test_missing_data_is_unavailable_and_never_zero(self):
        for raw in (None, {}, [], {"cedar_ember": None},
                    {"cedar_ember": {"eligible": False}},
                    {"cedar_ember": {"eligible": True, "grants": None}}):
            self.assertEqual(resetgrants.claude(raw, NOW), resetgrants.unavailable())
        for raw in (None, {}, [], {"available_count": True}, {"available_count": -1}):
            self.assertEqual(resetgrants.codex(raw, NOW), resetgrants.unavailable())

    def test_authoritative_codex_count_and_capped_list(self):
        block = resetgrants.codex({"available_count": 4, "credits": [credit()]}, NOW)
        self.assertEqual(block["count"], 4)
        self.assertFalse(block["details_complete"])
        self.assertEqual(len(block["grants"]), 1)
        block = resetgrants.codex(None, NOW, summary={"available_count": 3})
        self.assertEqual(block["count"], 3)
        self.assertEqual(block["grants"], [])

    def test_weekly_and_fable_grants_are_distinct_and_sanitized(self):
        rows = [claude_grant(clears=["seven_day", "seven_day_overage_included"]),
                claude_grant("grant-b", clears=["seven_day_overage_included"], label="private label")]
        block = observation(rows)["claude"]["meta"]["banked_resets"]
        self.assertEqual(block["count"], 4)
        self.assertEqual([g["coverage"] for g in block["grants"]], ["weekly_fable", "fable"])
        serialized = json.dumps(block)
        for private in ("grant-a", "grant-b", "private label", "resets_total"):
            self.assertNotIn(private, serialized)
        first_id = block["grants"][0]["id"]
        rows[0]["resets_left"] = 1
        self.assertEqual(observation(rows)["claude"]["meta"]["banked_resets"]["grants"][0]["id"], first_id)

    def test_codex_profile_and_redemption_data_are_discarded(self):
        block = resetgrants.codex({"available_count": 1, "credits": [credit(
            profile_user_id="private-user", profile_image_url="private-image", title="private-title")],
            "purchase_secret": "private-purchase"}, NOW)
        self.assertNotIn("private", json.dumps(block))
        self.assertNotIn("credit-a", json.dumps(block))

    def test_zero_expired_future_paused_and_unavailable_grants(self):
        rows = [claude_grant(left=0), claude_grant("expired", expires=NOW),
                claude_grant("future", starts_at=NOW + 1), claude_grant("paused", paused=True),
                claude_grant("unusable", usable_now=False)]
        b = observation(rows)["claude"]["meta"]["banked_resets"]
        self.assertEqual(b["count"], 0)
        self.assertEqual([g["status"] for g in b["grants"]],
                         ["consumed", "expired", "scheduled", "unavailable", "unavailable"])

    def test_invalid_counts_and_duplicate_rows(self):
        for value in (None, "2", -1, True, 1.2):
            self.assertIsNone(observation([claude_grant(left=value)])["claude"]["meta"]["banked_resets"]["count"])
        b = observation([claude_grant(), claude_grant()])["claude"]["meta"]["banked_resets"]
        self.assertEqual(b["count"], 2)

    def test_missing_identity_or_expiry_retains_count_but_cannot_remind(self):
        for row in (claude_grant(identity=None), claude_grant(expires=None),
                    claude_grant(expires="bad"), claude_grant(expires=float("nan"))):
            providers = observation([row])
            self.assertEqual(providers["claude"]["meta"]["banked_resets"]["count"], 2)
            self.assertEqual(resetwatch.AlertStore("unused").process_expiry(providers, NOW), [])


class TestExpiryReminders(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = os.path.join(self.folder.name, "state.json")
        self.store = resetwatch.AlertStore(self.path)

    def restart(self):
        self.assertTrue(self.store.save())
        self.store = resetwatch.AlertStore(self.path).load()

    def test_boundaries_and_late_startup(self):
        for seconds, threshold in ((36_001, None), (36_000, 10), (3601, 10),
                                   (3600, 1), (3599, 1), (1, 1), (0, None), (-1, None)):
            with self.subTest(seconds=seconds):
                store = resetwatch.AlertStore(self.path)
                rows = store.process_expiry(observation([claude_grant(expires=NOW + seconds)]), NOW)
                self.assertEqual([r["threshold_hours"] for r in rows], [] if threshold is None else [threshold])
                if threshold == 1:
                    self.assertEqual(list(store.delivered.values()), [[10, 1]])

    def test_dismiss_restart_and_both_thresholds(self):
        first = self.store.process_expiry(observation(), NOW)[0]
        self.assertEqual(first["threshold_hours"], 10)
        self.store.dismiss(first["id"])
        self.restart()
        self.assertEqual(self.store.process_expiry(observation(stamp=NOW + 1), NOW + 1), [])
        later = EXPIRY - 3600
        one = self.store.process_expiry(observation(stamp=later), later)
        self.assertEqual([r["threshold_hours"] for r in one], [1])
        self.store.dismiss_all()
        self.restart()
        self.assertEqual(self.store.process_expiry(observation(stamp=later + 1), later + 1), [])

    def test_undismissed_row_is_updated_without_duplication(self):
        first = self.store.process_expiry(observation(), NOW)[0]
        self.restart()
        later = EXPIRY - 3600
        events = self.store.process_expiry(observation([claude_grant(left=1)], later), later)
        self.assertEqual(len(self.store.pending), 1)
        self.assertEqual(events[0]["id"], first["id"])
        self.assertEqual(events[0]["remaining_count"], 1)
        self.assertEqual(events[0]["threshold_hours"], 1)
        self.assertEqual(self.store.process_expiry(observation(stamp=later + 1), later + 1), [])

    def test_consumption_between_reminders_suppresses_one_hour_stage(self):
        self.store.process_expiry(observation(), NOW)
        later = EXPIRY - 3600
        self.assertEqual(self.store.process_expiry(observation([claude_grant(left=0)], later), later), [])
        self.assertEqual(self.store.pending[0]["status"], "unavailable")
        self.assertEqual(self.store.pending[0]["remaining_count"], 0)
        self.restart()
        self.assertEqual(len(self.store.pending), 1)

    def test_codex_consumed_and_unsupported_grants_do_not_remind(self):
        for extra in ({"status": "redeemed"}, {"status": "expired"},
                      {"status": "redeeming"}, {"is_supported_by_plan": False}):
            self.assertEqual(self.store.process_expiry(
                observation([credit(**extra)], provider="codex", total=1), NOW), [])

    def test_unavailable_stale_count_only_and_future_data_cannot_deliver(self):
        for stamp in (NOW - 121, NOW + 6):
            self.assertEqual(self.store.process_expiry(observation(stamp=stamp), NOW), [])
        self.assertEqual(self.store.process_expiry({"claude": {"meta": {
            "banked_resets": resetgrants.unavailable()}}}, NOW), [])
        self.assertEqual(self.store.process_expiry(observation([], provider="codex", total=3), NOW), [])

    def test_incomplete_list_does_not_confirm_absent_grant_or_remind_it(self):
        self.store.process_expiry(observation([credit()], provider="codex", total=3), NOW)
        later = EXPIRY - 3600
        self.assertEqual(self.store.process_expiry(
            observation([credit("other", expires=EXPIRY + 100_000)], later, "codex", 2), later), [])
        self.assertEqual(self.store.pending[0]["status"], "unverified")
        self.assertEqual(list(self.store.delivered.values()), [[10]])
        # A subsequent explicit observation of the same credit can still deliver.
        self.assertEqual(self.store.process_expiry(
            observation([credit()], later, "codex", 2), later)[0]["threshold_hours"], 1)

    def test_complete_list_and_authoritative_zero_confirm_unavailability(self):
        for total, rows in ((0, []), (1, [credit("other", expires=EXPIRY + 100_000)])):
            self.store = resetwatch.AlertStore(self.path)
            self.store.process_expiry(observation([credit()], provider="codex"), NOW)
            later = EXPIRY - 3600
            self.assertEqual(self.store.process_expiry(observation(rows, later, "codex", total), later), [])
            self.assertEqual(self.store.pending[0]["status"], "unavailable")

    def test_grouping_and_independent_delivery_for_each_grant(self):
        rows = [claude_grant(), claude_grant("b", left=1),
                claude_grant("f", clears=["seven_day_overage_included"])]
        events = self.store.process_expiry(observation(rows), NOW)
        self.assertEqual(sorted(r["remaining_count"] for r in events), [2, 3])
        self.assertEqual(len(self.store.delivered), 3)
        later = EXPIRY - 3600
        rows[0]["resets_left"] = 0
        events = self.store.process_expiry(observation(rows, later), later)
        self.assertEqual(sorted(r["remaining_count"] for r in events), [1, 2])
        self.assertEqual(len(self.store.pending), 2)

    def test_providers_and_different_expirations_stay_separate(self):
        providers = observation([claude_grant(), claude_grant("b", expires=EXPIRY - 1)])
        providers.update(observation([credit()], provider="codex"))
        self.assertEqual(len(self.store.process_expiry(providers, NOW)), 3)

    def test_pending_expiry_updates_without_a_network_read_and_survives_restart(self):
        self.store.process_expiry(observation(), NOW)
        self.restart()
        self.assertEqual(self.store.process_expiry({}, EXPIRY), [])
        self.assertEqual(self.store.pending[0]["status"], "expired")
        self.restart()
        self.assertEqual(self.store.pending[0]["status"], "expired")

    def test_old_state_migrates_weekly_notices_and_preserves_baseline(self):
        with open(self.path, "w") as f:
            json.dump({"seen": {"claude": {"resets_at": 1000, "remaining_pct": 20}},
                       "pending": [{"id": "old", "provider": "claude", "from_pct": 5, "to_pct": 100}]}, f)
        self.store.load()
        self.assertEqual(self.store.pending[0]["kind"], "weekly_reset")
        self.assertEqual(self.store.delivered, {})
        events = resetwatch.detect_resets(self.store.seen, {
            "claude": {"resets_at": 9000, "remaining_pct": 100}})
        self.store.add(events)
        self.store.process_expiry(observation(), NOW)
        self.assertEqual(len(self.store.pending), 3)
        self.assertEqual(events[0]["kind"], "weekly_reset")

    def test_invalid_expiry_rows_cannot_poison_weekly_detection(self):
        with open(self.path, "w") as f:
            json.dump({"pending": [{"id": "old"}, {"kind": "reset_expiry", "id": "bad"}],
                       "delivered": {"valid": [10, 1, "bad"], "bad": 10}}, f)
        self.store.load()
        self.assertEqual([e["id"] for e in self.store.pending], ["old"])
        self.assertEqual(self.store.delivered, {"valid": [10, 1]})

    def test_capped_list_never_carries_unverified_grants_into_final_hour_count(self):
        rows = [credit("a"), credit("b")]
        self.store.process_expiry(observation(rows, provider="codex", total=3), NOW)
        later = EXPIRY - 3600
        events = self.store.process_expiry(observation([credit("b")], later, "codex", 2), later)
        self.assertEqual(events[0]["remaining_count"], 1)
        self.assertEqual(sorted(self.store.delivered.values()), [[10], [10, 1]])


class TestGrantCollection(unittest.TestCase):
    def test_cli_version_is_hidden_bounded_and_cached_even_on_failure(self):
        for outcome, expected in ((mock.Mock(returncode=0, stdout="2.1.323 (Claude Code)\n"), "2.1.323"),
                                  (mock.Mock(returncode=0, stdout="unexpected output"), None),
                                  (subprocess.TimeoutExpired("claude", 3), None), (OSError(), None)):
            widget.claude_cli_version.cache_clear()
            with mock.patch.object(widget.shutil, "which", return_value="claude.exe"), mock.patch.object(
                    widget.subprocess, "run", **({"side_effect": outcome} if isinstance(outcome, Exception)
                                               else {"return_value": outcome})) as run:
                self.assertEqual(widget.claude_cli_version(), expected)
                self.assertEqual(widget.claude_cli_version(), expected)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.kwargs["timeout"], 3)
                self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        widget.claude_cli_version.cache_clear()

    def test_claude_uses_verified_headers_and_retains_extra_usage(self):
        data = {"seven_day": {"utilization": 5}, "extra_usage": {"used_credits": 2},
                "cedar_ember": {"eligible": True, "grants": [claude_grant()]}}
        with mock.patch.object(widget, "read_claude_credentials", return_value=({"token": "secret"}, None)), \
                mock.patch.object(widget, "claude_cli_version", return_value="2.1.323"), \
                mock.patch.object(widget, "http_get_json", return_value=data) as get:
            result = widget.fetch_claude()
        self.assertTrue(result["ok"])
        self.assertIn("?cedar_ember=1", get.call_args.args[0])
        self.assertNotIn("skip_spend", get.call_args.args[0])
        self.assertEqual(get.call_args.args[1]["User-Agent"], "claude-cli/2.1.323 (external, cli)")
        self.assertEqual(get.call_args.args[1]["anthropic-beta"], "oauth-2025-04-20")
        self.assertIn("extra_usage", result["meta"])

    def test_optional_claude_read_failure_or_version_failure_keeps_quotas(self):
        for version in (None, "2.1.323"):
            def get(url, headers):
                if "?" in url:
                    raise OSError("optional read failed")
                return {"seven_day": {"utilization": 5}}
            with mock.patch.object(widget, "read_claude_credentials", return_value=({"token": "secret"}, None)), \
                    mock.patch.object(widget, "claude_cli_version", return_value=version), \
                    mock.patch.object(widget, "http_get_json", side_effect=get):
                result = widget.fetch_claude()
            self.assertTrue(result["ok"])
            self.assertIsNone(result["meta"]["banked_resets"]["count"])

    def test_codex_details_are_independent_of_usage_404_and_session_fallback(self):
        def get(url, headers):
            if url.endswith("rate-limit-reset-credits"):
                self.assertEqual(headers["chatgpt-account-id"], "account")
                return {"available_count": 3, "credits": [credit()]}
            raise widget.urllib.error.HTTPError(url, 404, "missing", None, None)
        with mock.patch.object(widget, "read_codex_credentials", return_value=({"token": "secret", "account_id": "account"}, None)), \
                mock.patch.object(widget, "http_get_json", side_effect=get), \
                mock.patch.object(widget, "read_codex_session_limits", return_value=({"secondary": {"used_percent": 5}}, NOW)):
            result = widget.fetch_codex()
        self.assertTrue(result["ok"])
        self.assertEqual(result["meta"]["usage_source"], "codex_session")
        self.assertEqual(result["meta"]["banked_resets"]["count"], 3)
        self.assertNotIn("secret", json.dumps(result))

    def test_loopback_feed_does_not_gain_reset_metadata(self):
        with mock.patch.object(widget, "STATE", widget.State()):
            widget.STATE.snapshot = {"providers": observation(), "updated_at": NOW}
            self.assertNotIn("banked_resets", json.dumps(widget.control_usage_snapshot()))

    def test_codex_optional_detail_failure_keeps_quotas_and_count(self):
        def get(url, headers):
            if url.endswith("rate-limit-reset-credits"):
                raise OSError("optional details unavailable")
            return {"rate_limit": {"secondary_window": {"used_percent": 10}},
                    "rate_limit_reset_credits": {"available_count": 4}}
        with mock.patch.object(widget, "read_codex_credentials", return_value=({"token": "secret"}, None)), \
                mock.patch.object(widget, "http_get_json", side_effect=get):
            result = widget.fetch_codex()
        self.assertTrue(result["ok"])
        self.assertEqual(result["meta"]["banked_resets"]["count"], 4)
        self.assertEqual(result["meta"]["banked_resets"]["grants"], [])

    def test_persistent_window_creation_still_does_not_take_focus(self):
        store = resetwatch.AlertStore("unused")
        store.process_expiry(observation(), NOW)
        manager = widget.AlertWindowManager()
        with mock.patch.object(widget, "ALERTS", store), \
                mock.patch.object(widget, "webview", mock.Mock(windows=[]), create=True) as view:
            manager.raise_alert()
        self.assertIs(view.create_window.call_args.kwargs["focus"], False)
        self.assertIs(view.create_window.call_args.kwargs["on_top"], True)

    def test_shared_toggle_and_notification_stages_use_existing_window(self):
        with tempfile.TemporaryDirectory() as folder:
            store = resetwatch.AlertStore(os.path.join(folder, "alerts.json"))
            with mock.patch.object(widget, "ALERTS", store), \
                    mock.patch.object(widget, "CFG", widget.normalize_config({})), \
                    mock.patch.object(widget, "_FIRST_COMPARE", True), \
                    mock.patch.object(widget, "ALERT_WINDOW") as window, \
                    mock.patch.object(resetwatch.time, "time", return_value=NOW):
                widget.process_reset_alerts(observation())
                self.assertEqual(window.toast.call_args.args[0][0]["threshold_hours"], 10)
                self.assertTrue(window.raise_alert.called)
                widget.CFG["reset_alert"]["enabled"] = False
                widget.process_reset_alerts(observation())
                self.assertEqual(store.pending, [])
                self.assertTrue(window.refit_or_close.called)
                self.assertTrue(store.delivered)
                widget.CFG["reset_alert"]["enabled"] = True
                later = EXPIRY - 3600
                with mock.patch.object(resetwatch.time, "time", return_value=later):
                    widget.process_reset_alerts(observation(stamp=later))
                self.assertEqual(window.toast.call_args.args[0][0]["threshold_hours"], 1)

    def test_toasts_have_provider_type_count_and_local_expiration_in_both_languages(self):
        event = resetwatch.AlertStore("unused").process_expiry(observation(), NOW)[0]
        for lang in ("en", "ru"):
            with mock.patch.object(widget, "CFG", widget.normalize_config({"language": lang})), \
                    mock.patch.object(widget, "TRAY_AVAILABLE", True), \
                    mock.patch.object(widget.TRAY, "icon") as icon:
                widget.AlertWindowManager().toast([event])
            body = icon.notify.call_args.args[0]
            self.assertIn("Claude Code", body)
            self.assertIn("2", body)
            self.assertIn("10", body)
            self.assertIn("2033", body)
            self.assertNotIn("%(count)", body)

    def test_multiple_expiry_groups_get_individual_toasts(self):
        events = resetwatch.AlertStore("unused").process_expiry(observation([
            claude_grant(), claude_grant("b", expires=EXPIRY - 1),
            claude_grant("f", clears=["seven_day_overage_included"])]), NOW)
        with mock.patch.object(widget, "TRAY_AVAILABLE", True), \
                mock.patch.object(widget.TRAY, "icon") as icon:
            widget.AlertWindowManager().toast(events)
        self.assertEqual(icon.notify.call_count, 3)
        for call in icon.notify.call_args_list:
            self.assertLess(len(call.args[0]), 256)


if __name__ == "__main__":
    unittest.main()
