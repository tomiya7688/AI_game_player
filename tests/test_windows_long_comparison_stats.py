import inspect
import unittest

from tools import windows_long_comparison_stats as stats


def episode(**overrides):
    base = {
        "episode": 1,
        "seed": 479,
        "episode_status": "completed",
        "terminal": True,
        "stop_reason": "terminal",
        "actions": 10,
        "attempts": 12,
        "holds": 2,
        "invalid_no_op_actions": 1,
        "board_changes": 9,
        "policy_reobservations": 3,
        "nodes": 500,
        "score": 1000,
        "max_tile": 128,
        "latency_ms": [1.0, 2.0, 3.0, 4.0],
        "resources": {
            "parent_cpu_seconds": 1.0,
            "child_cpu_seconds_sampled_lower_bound": 2.0,
            "process_tree_peak_rss_bytes_sampled": 1000,
            "sampling_error": None,
        },
        "wall_seconds": 5.0,
        "seed_fixture": {"seed": 479},
        "planner_readiness": None,
        "failure_stage": None,
        "failure": None,
        "cleanup_failure": None,
        "stopped_input_rejected": True,
        "stale_generation_rejected": True,
    }
    return {**base, **overrides}


class MetricTypeTests(unittest.TestCase):
    def test_null_cannot_be_measured_and_unknown_never_becomes_zero(self):
        self.assertTrue(stats.validate_metric({"value": None, "status": "measured"}))
        self.assertTrue(stats.validate_metric({"value": 0, "status": "unknown"}))
        self.assertTrue(stats.validate_metric({"value": True, "status": "measured"}))
        self.assertEqual([], stats.validate_metric(stats.unknown()))
        self.assertEqual([], stats.validate_metric(stats.not_applicable()))
        self.assertEqual([], stats.validate_metric(stats.measured(0)))
        with self.assertRaises(ValueError):
            stats.measured(None)


class RunMetricsTests(unittest.TestCase):
    def test_cycling_and_baseline_nodes_and_reobserve_are_not_applicable(self):
        metrics = stats.run_metrics("cycling", [episode(nodes=None)])
        for name in ("nodes", "reobserve_count", "reobserve_rate"):
            self.assertEqual(metrics[name]["status"], "not_applicable")
        self.assertEqual(metrics["copy_io"]["status"], "unknown")

    def test_planner_nodes_unknown_without_counters_and_measured_with(self):
        unreceived = stats.run_metrics("planner", [episode(nodes=None)])
        self.assertEqual(unreceived["nodes"]["status"], "unknown")
        metrics = stats.run_metrics("planner", [episode(nodes=7), episode(nodes=5)])
        self.assertEqual(metrics["nodes"], {"value": 12, "status": "measured"})

    def test_unreadable_score_is_unknown_not_zero(self):
        metrics = stats.run_metrics("cycling", [episode(score=None)])
        self.assertEqual(metrics["episode_mean_score"]["status"], "unknown")
        self.assertEqual(metrics["score_episodes_measured"], 0)
        mixed = stats.run_metrics("cycling", [episode(score=None), episode(score=300)])
        self.assertEqual(mixed["episode_mean_score"]["value"], 300)
        self.assertEqual(mixed["score_episodes_measured"], 1)

    def test_survival_counts_only_terminal_episodes_and_keeps_right_censoring(self):
        censored_only = stats.run_metrics(
            "cycling", [episode(terminal=False, actions=90)]
        )
        self.assertEqual(censored_only["survival_median_actions"]["status"], "unknown")
        self.assertEqual(censored_only["censored_episodes"], 1)
        mixed = stats.run_metrics(
            "cycling",
            [
                episode(actions=10),
                episode(actions=30),
                episode(terminal=False, actions=999),
            ],
        )
        self.assertEqual(mixed["survival_median_actions"]["value"], 20)
        self.assertEqual(
            (mixed["terminal_episodes"], mixed["censored_episodes"]), (2, 1)
        )

    def test_failed_episode_with_unknown_terminal_is_not_right_censored(self):
        metrics = stats.run_metrics(
            "cycling", [episode(terminal=None), episode(terminal=False)]
        )
        self.assertEqual(metrics["censored_episodes"], 1)
        self.assertEqual(metrics["terminal_unknown_episodes"], 1)

    def test_wilson_rejects_impossible_counts(self):
        with self.assertRaises(ValueError):
            stats.wilson_interval(9, 8)

    def test_latency_percentiles_unknown_without_samples_and_nearest_rank_with(self):
        empty = stats.run_metrics("cycling", [episode(latency_ms=[])])
        self.assertEqual(empty["latency_p50_ms"]["status"], "unknown")
        samples = [float(i) for i in range(1, 21)]
        metrics = stats.run_metrics("cycling", [episode(latency_ms=samples)])
        self.assertEqual(metrics["latency_p50_ms"]["value"], 10.0)
        self.assertEqual(metrics["latency_p95_ms"]["value"], 19.0)

    def test_resources_unknown_when_any_sampling_failed(self):
        bad = episode(resources={"sampling_error": "Error"})
        metrics = stats.run_metrics("cycling", [episode(), bad])
        self.assertEqual(metrics["cpu_seconds"]["status"], "unknown")
        self.assertEqual(metrics["peak_rss_bytes"]["status"], "unknown")
        good = stats.run_metrics("cycling", [episode()])
        self.assertEqual(good["cpu_seconds"]["value"], 3.0)

    def test_rates_use_actions_and_attempts_and_unknown_for_zero_denominator(self):
        zero = stats.run_metrics(
            "cycling",
            [episode(actions=0, attempts=0, holds=0, invalid_no_op_actions=0)],
        )
        self.assertEqual(zero["invalid_no_op_rate"]["status"], "unknown")
        self.assertEqual(zero["abstain_rate"]["status"], "unknown")
        normal = stats.run_metrics("cycling", [episode()])
        self.assertAlmostEqual(normal["abstain_rate"]["value"], 2 / 12)


class ClassifyRunTests(unittest.TestCase):
    def test_status_comes_from_state_values(self):
        self.assertEqual(
            stats.classify_run([episode()], "wall_budget_reached"),
            ("completed", "wall_budget_reached"),
        )
        self.assertEqual(
            stats.classify_run([episode(actions=0)], "wall_budget_reached")[0], "failed"
        )
        failed = episode(episode_status="failed", failure_stage="decision_execution")
        self.assertEqual(
            stats.classify_run([episode(), failed], "wall_budget_reached")[0], "partial"
        )
        self.assertEqual(
            stats.classify_run([episode()], "recovery_exhausted")[0], "failed"
        )
        self.assertEqual(stats.classify_run([episode()], "hard_timeout")[0], "failed")
        probe = episode(episode_status="failed", failure_stage="stale_generation_probe")
        self.assertEqual(
            stats.classify_run([probe], "wall_budget_reached"),
            ("failed", "fatal:stale_generation_probe"),
        )


class StatisticsTests(unittest.TestCase):
    def test_wilson_interval_is_deterministic_and_bounded(self):
        interval = stats.wilson_interval(8, 10)
        self.assertAlmostEqual(interval["lower"], 0.4902, places=3)
        self.assertAlmostEqual(interval["upper"], 0.9433, places=3)
        self.assertIsNone(stats.wilson_interval(0, 0)["rate"])

    def test_sign_flip_exact_and_monte_carlo_are_deterministic(self):
        self.assertAlmostEqual(stats.sign_flip_p_value([1.0] * 8), 2 / 256)
        self.assertEqual(stats.sign_flip_p_value([1.0, -1.0, 2.0, -2.0]), 1.0)
        big = [float(i % 5 + 1) for i in range(20)]
        self.assertEqual(stats.sign_flip_p_value(big), stats.sign_flip_p_value(big))
        self.assertLess(stats.sign_flip_p_value(big), 0.001)

    def test_bootstrap_interval_is_reproducible(self):
        data = [3.0, 5.0, 4.0, 6.0, 2.0, 7.0, 5.0, 4.0]
        self.assertEqual(
            stats.bootstrap_mean_interval(data), stats.bootstrap_mean_interval(data)
        )
        low, high = stats.bootstrap_mean_interval(data)
        self.assertTrue(0 < low < sum(data) / 8 < high)

    def test_fewer_than_eight_pairs_gives_no_interval_and_inconclusive(self):
        result = stats.paired_contrast([5.0] * 7)
        self.assertEqual(result["status"], "insufficient_samples")
        self.assertIsNone(result["interval"])
        self.assertEqual(result["verdict"], "inconclusive")

    def test_eight_consistent_pairs_can_be_better_or_worse(self):
        up = stats.paired_contrast([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        down = stats.paired_contrast([-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -7.0, -8.0])
        self.assertEqual(up["verdict"], "better")
        self.assertEqual(down["verdict"], "worse")

    def test_interval_containing_zero_is_inconclusive_even_with_eight_pairs(self):
        result = stats.paired_contrast([5.0, -4.0, 3.0, -6.0, 4.0, -3.0, 6.0, -5.0])
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["verdict"], "inconclusive")

    def test_exploratory_contrast_has_no_verdict_key(self):
        self.assertNotIn(
            "verdict", stats.paired_contrast([1.0] * 8, with_verdict=False)
        )

    def test_unknown_exclusions_are_counted(self):
        self.assertEqual(stats.paired_contrast([1.0], 3)["n_excluded_unknown"], 3)

    def test_thresholds_are_constants_not_parameters(self):
        self.assertEqual(stats.N_MIN_VERDICT, 8)
        self.assertAlmostEqual(stats.CORRECTED_ALPHA, 0.05 / 3)
        self.assertEqual((stats.BOOTSTRAP_RESAMPLES, stats.RANDOM_SEED), (10000, 795))
        tunable = {"n_min", "alpha", "resamples", "seed", "confidence", "threshold"}
        for function in (
            stats.paired_contrast,
            stats.bootstrap_mean_interval,
            stats.sign_flip_p_value,
        ):
            self.assertFalse(set(inspect.signature(function).parameters) & tunable)


if __name__ == "__main__":
    unittest.main()
