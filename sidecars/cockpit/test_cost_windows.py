"""Regression tests for pricing full quota buckets before their timer starts."""
import unittest

from cost_windows import HOURS_5, WEEK, WindowCosts, attach_quota_costs, merge_quota_cache, quota_status_fields, quota_window
from quota_sources import codex_rollout_windows, codex_rpc_windows

NOW = 1_800_000_000.0
BID = "3p-weekly"
OBSERVED = NOW - 600


class FullQuotaWindowTests(unittest.TestCase):
    def test_history_is_excluded_and_new_cost_counts(self):
        for reset in ({}, {"reset_ts": 0.0}):
            with self.subTest(reset=reset):
                info = dict(reset, rem_frac=1.0, observed_at=OBSERVED)
                costs = WindowCosts({BID: info}, NOW)
                costs.add("agy_tr", "claude", NOW - 2 * 86400, 71.78, "old")
                costs.add("agy_tr", "claude", OBSERVED - 1, 3.0, "pre-observation")
                self.assertEqual(costs.totals[BID], 0)
                costs.add("agy_tr", "claude", OBSERVED + 1, 2.5, "new")
                self.assertEqual(costs.totals[BID], 2.5)
                out = attach_quota_costs({}, (BID,), costs.totals)
                self.assertEqual(out["cost-5h"]["usd"], 5.5)
                self.assertAlmostEqual(out["cost-weekly"]["usd"], 77.28)
                self.assertFalse(costs.windows[BID].boundary_known)
                self.assertFalse(costs.windows[BID].trusted)
                estimate = costs.eligible_estimates().get(BID)
                self.assertIsNone(estimate)
                fields = quota_status_fields(BID, info, NOW, estimate)
                self.assertTrue(fields["window_boundary_uncertain"])
                self.assertIs(fields["estimate_ok"], False)
                self.assertIsNone(fields["estimate_usd"])
                self.assertIsNone(fields.get("estimate_total_usd"))

    def test_observations_beyond_window_are_clamped(self):
        for bid, length in ((BID, WEEK), ("3p-5h", HOURS_5)):
            with self.subTest(bid=bid):
                start = NOW - length
                info = {"rem_frac": 1.0, "reset_ts": 0, "observed_at": start - 60, "stale": True}
                costs = WindowCosts({bid: info}, NOW)
                self.assertEqual(costs.windows[bid].start, start)
                costs.add("agy_tr", "claude", start - 1, 13, "outside")
                costs.add("agy_tr", "claude", start, 2.5, "at-cutoff")
                self.assertEqual(costs.totals[bid], 2.5)

    def test_missing_or_invalid_observation_is_stale_with_or_without_flag(self):
        for observation in ({}, *({"observed_at": value} for value in
                              (None, True, str(NOW), 0, float("nan"), float("inf"), NOW + 2.01))):
            with self.subTest(observation=observation):
                info = dict(observation, rem_frac=1.0, reset_ts=0)
                amounts = []
                for snapshot in (info, dict(info, stale=True)):
                    costs = WindowCosts({BID: snapshot}, NOW)
                    self.assertEqual(costs.windows[BID].start, NOW - WEEK)
                    self.assertTrue(quota_status_fields(BID, snapshot, NOW, None)["stale"])
                    costs.add("agy_tr", "claude", NOW - 2 * 86400, 71.78, "old")
                    amounts.append(costs.totals[BID])
                self.assertEqual(amounts, [71.78, 71.78])
        info = {"rem_frac": 1.0, "reset_ts": 0, "observed_at": NOW + 2}
        self.assertEqual(quota_window(BID, info, NOW).start, NOW + 2)

    def test_stale_but_valid_observation_still_anchors_spending(self):
        anchor = NOW - 1801
        info = {"rem_frac": 1.0, "reset_ts": 0, "observed_at": anchor}
        self.assertTrue(quota_status_fields(BID, info, NOW, None)["stale"])
        costs = WindowCosts({BID: info}, NOW)
        costs.add("agy_tr", "claude", anchor - 1, 71.78, "before-observation")
        costs.add("agy_tr", "claude", anchor + 1, 2.5, "after-observation")
        self.assertEqual(costs.totals[BID], 2.5)

    def test_existing_period_and_rolling_fallbacks_are_preserved(self):
        future_reset = NOW + WEEK - 7200
        cases = (
            ({"rem_frac": 1.0, "reset_ts": future_reset}, NOW - 7200, 3, True),
            ({"rem_frac": 1.0, "reset_ts": NOW - 1}, NOW - WEEK, 74.78, False),
            ({"rem_frac": 1.0, "reset_ts": NOW + WEEK + 1}, NOW - WEEK, 74.78, False),
            ({"rem_frac": 0.7, "reset_ts": 0}, NOW - WEEK, 74.78, False),
        )
        for info, start, usd, known in cases:
            with self.subTest(info=info):
                costs = WindowCosts({BID: dict(info, observed_at=OBSERVED)}, NOW)
                self.assertEqual(costs.windows[BID].start, start)
                self.assertIs(costs.windows[BID].boundary_known, known)
                costs.add("agy_tr", "claude", NOW - 2 * 86400, 71.78, "old")
                costs.add("agy_tr", "claude", NOW - 3600, 3, "inside-period")
                self.assertAlmostEqual(costs.totals[BID], usd)

    def test_invalid_remaining_or_reset_does_not_activate_full_bucket_rule(self):
        for rem in (True, None, float("nan"), float("inf"), "1.0"):
            with self.subTest(rem=rem):
                info = {"rem_frac": rem, "reset_ts": 0, "observed_at": OBSERVED}
                self.assertEqual(quota_window(BID, info, NOW).start, NOW - WEEK)
        for reset in (False, "bad", float("nan"), float("inf"), -float("inf")):
            with self.subTest(reset=reset):
                info = {"rem_frac": 1.0, "reset_ts": reset, "observed_at": OBSERVED}
                self.assertEqual(quota_window(BID, info, NOW).start, NOW - WEEK)

    def test_provider_specific_boundaries(self):
        full = {"rem_frac": 1.0, "reset_ts": 0, "observed_at": OBSERVED}
        self.assertEqual(quota_window("grok-weekly", full, NOW).start, OBSERVED)
        period = dict(full, period_start_ts=NOW - 86400, reset_ts=NOW + 86400)
        window = quota_window("grok-weekly", period, NOW)
        self.assertEqual((window.start, window.end), (period["period_start_ts"], period["reset_ts"]))
        self.assertTrue(window.boundary_known)
        self.assertEqual(quota_window("codex-weekly", full, NOW).start, NOW - WEEK)
        self.assertEqual(quota_status_fields("codex-weekly", full, NOW, None)["usd_scope"], "legacy_rolling")
        droid = dict(full, observed_at=NOW - WEEK - 1)
        self.assertEqual(quota_window("droid-weekly", droid, NOW).start, droid["observed_at"])

    def test_grok_full_bucket_honors_both_available_start_bounds(self):
        bid = "grok-weekly"
        period = {"rem_frac": 1.0, "reset_ts": 0, "period_start_ts": NOW - 86400}
        cases = (
            (dict(period, observed_at=OBSERVED), 2.5),
            (dict(period, period_start_ts=NOW + 1, observed_at=OBSERVED), 2.5),
            (period, 5.5),
            (dict(period, observed_at=None, stale=True), 5.5),
            (dict(period, period_start_ts=OBSERVED + 2, observed_at=OBSERVED), 2),
        )
        for info, usd in cases:
            with self.subTest(info=info):
                costs = WindowCosts({bid: info}, NOW)
                for ts, amount in ((NOW - 2 * 86400, 71.78), (OBSERVED - 1, 3),
                                   (OBSERVED + 1, 0.5), (OBSERVED + 3, 2)):
                    costs.add("grok", "", ts, amount, str(ts))
                self.assertEqual(costs.totals[bid], usd)
                self.assertFalse(costs.windows[bid].boundary_known)

    def test_codex_full_rpc_replaces_only_expired_matching_window(self):
        bid = "codex-weekly"

        def rpc(used, observed, reset=0):
            return codex_rpc_windows({"rateLimits": {"limitId": "codex", "secondary": {
                "usedPercent": used, "windowDurationMins": 10080, "resetsAt": reset,
            }}}, observed)

        fresh = rpc(0, NOW)
        for old_reset in (NOW - 600, NOW):
            with self.subTest(old_reset=old_reset):
                expired = rpc(90, NOW - 1200, old_reset)
                merged = merge_quota_cache(expired, fresh)
                self.assertEqual(merged[bid], fresh[bid])
                costs = WindowCosts(merged, NOW + 30)
                costs.add("codex", "", NOW - 2 * 86400, 71.78, "old")
                self.assertEqual(costs.totals[bid], 0)
                costs.add("codex", "", NOW + 5, 2.5, "new")
                self.assertEqual(costs.totals[bid], 2.5)

        expired = rpc(90, NOW - 1200, NOW - 600)
        rollout = codex_rollout_windows({"limit_id": "codex", "secondary": {
            "used_percent": 0, "window_minutes": 10080, "resets_at": 0,
        }}, NOW)
        for previous, incoming in (
            (rpc(90, NOW - 1200, NOW + 1), fresh),
            (expired, rollout),
            (expired, rpc(10, NOW)),
            (expired, rpc(0, NOW - 1200)),
            ({bid: dict(expired[bid], limit_id="other")}, fresh),
            ({bid: dict(expired[bid], quota_slot="primary")}, fresh),
        ):
            with self.subTest(previous=previous, incoming=incoming):
                self.assertEqual(merge_quota_cache(previous, incoming)[bid], previous[bid])

    def test_ttl_stale_active_window_keeps_estimate_while_reset_passed_clears(self):
        bid = "3p-weekly"
        stale_observed = NOW - 3600
        active_info = {"rem_frac": 0.8, "reset_ts": NOW + WEEK - 7200, "observed_at": stale_observed}
        active_costs = WindowCosts({bid: active_info}, NOW)
        active_costs.add("agy_tr", "claude", NOW - 3600, 12.0, "in-win")
        active_fields = quota_status_fields(bid, active_info, NOW, active_costs.eligible_estimates().get(bid))
        self.assertTrue(active_fields["stale"])
        self.assertIs(active_fields["estimate_ok"], True)
        self.assertAlmostEqual(active_fields["estimate_usd"], 12.0)

        expired_info = {"rem_frac": 0.8, "reset_ts": NOW - 10, "observed_at": stale_observed}
        expired_costs = WindowCosts({bid: expired_info}, NOW)
        expired_fields = quota_status_fields(bid, expired_info, NOW, expired_costs.eligible_estimates().get(bid))
        self.assertTrue(expired_fields["stale"])
        self.assertIs(expired_fields["estimate_ok"], False)
        self.assertIsNone(expired_fields["estimate_usd"])


if __name__ == "__main__":
    unittest.main()
