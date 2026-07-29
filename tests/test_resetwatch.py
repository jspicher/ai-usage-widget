import unittest

import resetwatch


def provider(pid="claude", ok=True, resets_at=1000.0, pct=20.0, wid="week"):
    """Build a provider dict shaped like widget.py's fetch_* output."""
    return {
        "id": pid, "name": pid, "ok": ok, "meta": {}, "error": None,
        "windows": [
            {"id": "session", "label": "s", "used_pct": 5.0,
             "remaining_pct": 95.0, "resets_at": 500.0},
            {"id": wid, "label": "w", "used_pct": 100.0 - pct,
             "remaining_pct": pct, "resets_at": resets_at},
        ],
    }


class TestReadings(unittest.TestCase):
    def test_extracts_only_the_week_window(self):
        r = resetwatch.readings({"claude": provider()})
        self.assertEqual(r, {"claude": {"resets_at": 1000.0, "remaining_pct": 20.0}})

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


if __name__ == "__main__":
    unittest.main()
