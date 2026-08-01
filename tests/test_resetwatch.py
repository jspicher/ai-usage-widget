import unittest
import json
import os
import tempfile
from unittest import mock

import resetwatch


def provider(pid="claude", ok=True, resets_at=1000.0, pct=20.0, wid="week",
             derived=False):
    """Build a provider dict shaped like widget.py's fetch_* output."""
    return {
        "id": pid, "name": pid, "ok": ok, "meta": {}, "error": None,
        "windows": [
            {"id": "session", "label": "s", "used_pct": 5.0,
             "remaining_pct": 95.0, "resets_at": 500.0},
            {"id": wid, "label": "w", "used_pct": 100.0 - pct,
             "remaining_pct": pct, "resets_at": resets_at,
             "extra": {"resets_at_derived": True} if derived else {}},
        ],
    }


class TestReadings(unittest.TestCase):
    def test_extracts_only_the_week_window(self):
        r = resetwatch.readings({"claude": provider()})
        self.assertEqual(r, {"claude": {"resets_at": 1000.0, "remaining_pct": 20.0,
                                        "resets_at_derived": False}})

    def test_carries_the_derived_timestamp_flag(self):
        r = resetwatch.readings({"codex": provider(pid="codex", derived=True)})
        self.assertTrue(r["codex"]["resets_at_derived"])

    def test_skips_provider_that_is_not_ok(self):
        self.assertEqual(resetwatch.readings({"claude": provider(ok=False)}), {})

    def test_skips_provider_with_no_week_window(self):
        self.assertEqual(resetwatch.readings({"claude": provider(wid="month")}), {})

    def test_skips_none_values(self):
        p = provider()
        p["windows"][1]["remaining_pct"] = None
        self.assertEqual(resetwatch.readings({"claude": p}), {})


class TestDetect(unittest.TestCase):
    def setUp(self):
        self.prev = {"claude": {"resets_at": 1000.0, "remaining_pct": 20.0}}

    def test_boundary_advance_fires(self):
        nxt = {"claude": {"resets_at": 1000.0 + 7200, "remaining_pct": 100.0}}
        events = resetwatch.detect_resets(self.prev, nxt, now=42.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["provider"], "claude")
        self.assertEqual(events[0]["from_pct"], 20.0)
        self.assertEqual(events[0]["to_pct"], 100.0)
        self.assertEqual(events[0]["detected_at"], 42.0)
        self.assertFalse(events[0]["while_away"])

    def test_pct_jump_fires_without_boundary_move(self):
        nxt = {"claude": {"resets_at": 1000.0, "remaining_pct": 80.0}}
        self.assertEqual(len(resetwatch.detect_resets(self.prev, nxt)), 1)

    def test_ordinary_time_passing_does_not_fire(self):
        nxt = {"claude": {"resets_at": 999.0, "remaining_pct": 19.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_sub_threshold_movement_does_not_fire(self):
        nxt = {"claude": {"resets_at": 1000.0 + 60, "remaining_pct": 25.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_boundary_moving_backwards_does_not_fire(self):
        nxt = {"claude": {"resets_at": 1000.0 - 7200, "remaining_pct": 20.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_clock_jump_does_not_fabricate_an_event(self):
        """A two-hour clock jump with no quota change must not emit an event.

        The timestamp was derived from now(), so it moved with the clock.
        Without ``resets_at_derived``, this would look like a shifted window
        boundary and produce a false weekly-quota-reset alert.
        """
        prev = {"codex": {"resets_at": 1000.0, "remaining_pct": 20.0,
                          "resets_at_derived": True}}
        nxt = {"codex": {"resets_at": 1000.0 + 7200, "remaining_pct": 21.0,
                         "resets_at_derived": True}}
        self.assertEqual(resetwatch.detect_resets(prev, nxt), [])

    def test_derived_timestamp_still_fires_on_a_pct_jump(self):
        """The balance signal is clock-independent and works for derived timestamps."""
        prev = {"codex": {"resets_at": 1000.0, "remaining_pct": 20.0,
                          "resets_at_derived": True}}
        nxt = {"codex": {"resets_at": 1000.0 + 7200, "remaining_pct": 100.0,
                         "resets_at_derived": True}}
        events = resetwatch.detect_resets(prev, nxt)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["to_pct"], 100.0)

    def test_one_derived_side_is_enough_to_drop_the_boundary_signal(self):
        """A derived flag on the new reading alone disables the boundary signal."""
        prev = {"codex": {"resets_at": 1000.0, "remaining_pct": 20.0}}
        nxt = {"codex": {"resets_at": 1000.0 + 7200, "remaining_pct": 20.0,
                         "resets_at_derived": True}}
        self.assertEqual(resetwatch.detect_resets(prev, nxt), [])

    def test_absolute_timestamps_keep_the_boundary_signal(self):
        """Absolute ``resets_at`` windows must retain boundary detection."""
        prev = {"claude": {"resets_at": 1000.0, "remaining_pct": 20.0,
                           "resets_at_derived": False}}
        nxt = {"claude": {"resets_at": 1000.0 + 7200, "remaining_pct": 20.0,
                          "resets_at_derived": False}}
        self.assertEqual(len(resetwatch.detect_resets(prev, nxt)), 1)

    def test_missing_provider_in_next_is_skipped(self):
        self.assertEqual(resetwatch.detect_resets(self.prev, {}), [])

    def test_first_sighting_seeds_without_alerting(self):
        nxt = {"codex": {"resets_at": 5000.0, "remaining_pct": 90.0}}
        self.assertEqual(resetwatch.detect_resets(self.prev, nxt), [])

    def test_while_away_flag_is_propagated(self):
        nxt = {"claude": {"resets_at": 1000.0 + 7200, "remaining_pct": 100.0}}
        events = resetwatch.detect_resets(self.prev, nxt, while_away=True)
        self.assertTrue(events[0]["while_away"])

    def test_thresholds_are_configurable(self):
        nxt = {"claude": {"resets_at": 1000.0, "remaining_pct": 25.0}}
        cfg = {"pct_jump_threshold": 5}
        self.assertEqual(len(resetwatch.detect_resets(self.prev, nxt, cfg)), 1)

    def test_same_reset_yields_a_stable_id(self):
        nxt = {"claude": {"resets_at": 8000.0, "remaining_pct": 100.0}}
        a = resetwatch.detect_resets(self.prev, nxt, now=1.0)[0]["id"]
        b = resetwatch.detect_resets(self.prev, nxt, now=999.0)[0]["id"]
        self.assertEqual(a, b)

    def test_different_resets_yield_different_ids(self):
        a = resetwatch.event_id("claude", 8000.0, 100.0)
        b = resetwatch.event_id("claude", 9000.0, 100.0)
        self.assertNotEqual(a, b)


class TestAlertStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "state.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_loads_empty(self):
        s = resetwatch.AlertStore(self.path).load()
        self.assertEqual(s.seen, {})
        self.assertEqual(s.pending, [])

    def test_corrupt_file_loads_empty_instead_of_raising(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        s = resetwatch.AlertStore(self.path).load()
        self.assertEqual(s.seen, {})

    def test_round_trip(self):
        s = resetwatch.AlertStore(self.path).load()
        s.merge_seen({"claude": {"resets_at": 1.0, "remaining_pct": 2.0}})
        s.add([{"id": "abc", "provider": "claude"}])
        self.assertTrue(s.save())
        again = resetwatch.AlertStore(self.path).load()
        self.assertEqual(again.seen["claude"]["remaining_pct"], 2.0)
        self.assertEqual(len(again.pending), 1)

    def test_save_leaves_no_temp_files(self):
        s = resetwatch.AlertStore(self.path).load()
        self.assertTrue(s.save())
        self.assertEqual(os.listdir(self.dir.name), ["state.json"])

    def test_save_reports_success(self):
        s = resetwatch.AlertStore(self.path).load()
        self.assertIs(s.save(), True)
        self.assertTrue(s.last_save_ok)

    def test_save_failure_returns_false_instead_of_raising(self):
        """mkstemp may fail for a missing directory, but save() must not raise."""
        bad = os.path.join(self.dir.name, "no-such-dir", "state.json")
        log = os.path.join(self.dir.name, "widget-error.log")
        s = resetwatch.AlertStore(bad, log_path=log)
        self.assertIs(s.save(), False)
        self.assertFalse(s.last_save_ok)

    def test_save_failure_is_logged(self):
        bad = os.path.join(self.dir.name, "no-such-dir", "state.json")
        log = os.path.join(self.dir.name, "widget-error.log")
        resetwatch.AlertStore(bad, log_path=log).save()
        with open(log, "r", encoding="utf-8") as f:
            line = f.read()
        self.assertIn("save failed", line)

    def test_logging_failure_cannot_raise(self):
        """An unavailable log must not cause an exception to escape."""
        bad = os.path.join(self.dir.name, "no-such-dir", "state.json")
        bad_log = os.path.join(self.dir.name, "no-such-dir", "widget-error.log")
        s = resetwatch.AlertStore(bad, log_path=bad_log)
        self.assertIs(s.save(), False)

    def test_unchanged_state_does_not_rewrite(self):
        """save() runs every poll; an idle machine must not fsync every time."""
        s = resetwatch.AlertStore(self.path).load()
        with mock.patch.object(resetwatch, "atomic_write_json") as write:
            self.assertTrue(s.save())
            self.assertTrue(s.save())
            self.assertEqual(write.call_count, 1)
            s.add([{"id": "x"}])
            self.assertTrue(s.save())
            self.assertEqual(write.call_count, 2)

    def test_failed_write_is_retried_on_the_next_save(self):
        """A skipped write must never be skipped because the last one failed."""
        s = resetwatch.AlertStore(self.path).load()
        s.add([{"id": "x"}])
        with mock.patch.object(
                resetwatch, "atomic_write_json", side_effect=OSError("boom")):
            self.assertFalse(s.save())
        with mock.patch.object(resetwatch, "atomic_write_json") as write:
            self.assertTrue(s.save())
            write.assert_called_once()

    def test_save_recovers_after_a_failure(self):
        s = resetwatch.AlertStore(self.path).load()
        s.last_save_ok = False
        self.assertIs(s.save(), True)
        self.assertTrue(s.last_save_ok)

    def test_log_path_defaults_next_to_the_state_file(self):
        s = resetwatch.AlertStore(self.path)
        self.assertEqual(os.path.dirname(s.log_path), self.dir.name)

    def test_merge_seen_preserves_absent_providers(self):
        s = resetwatch.AlertStore(self.path).load()
        s.merge_seen({"claude": {"resets_at": 1.0, "remaining_pct": 2.0},
                      "codex": {"resets_at": 3.0, "remaining_pct": 4.0}})
        s.merge_seen({"claude": {"resets_at": 9.0, "remaining_pct": 9.0}})
        self.assertEqual(s.seen["codex"]["resets_at"], 3.0)
        self.assertEqual(s.seen["claude"]["resets_at"], 9.0)

    def test_add_dedupes_by_id(self):
        s = resetwatch.AlertStore(self.path).load()
        self.assertEqual(len(s.add([{"id": "x"}])), 1)
        self.assertEqual(len(s.add([{"id": "x"}])), 0)
        self.assertEqual(len(s.pending), 1)

    def test_dismiss_removes_one(self):
        s = resetwatch.AlertStore(self.path).load()
        s.add([{"id": "x"}, {"id": "y"}])
        self.assertTrue(s.dismiss("x"))
        self.assertEqual([e["id"] for e in s.pending], ["y"])
        self.assertFalse(s.dismiss("nope"))

    def test_dismiss_all_clears(self):
        s = resetwatch.AlertStore(self.path).load()
        s.add([{"id": "x"}, {"id": "y"}])
        self.assertEqual(s.dismiss_all(), 2)
        self.assertEqual(s.pending, [])


if __name__ == "__main__":
    unittest.main()
