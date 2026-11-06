import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import compare_2048_windows_long as runner


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def identity(provider="cycling", base_seed=479, play_seconds=600):
    return {
        "track": runner.TRACK,
        "ref_class": "manual",
        "git_commit": "a" * 40,
        "provider": provider,
        "base_seed": base_seed,
        "block_index": 0,
        "provider_order_index": 0,
        "condition_fingerprint": "f" * 64,
        "hardware": {"cpu_model": "x", "logical_cores": 4},
        "budget": runner.budget_record(play_seconds),
    }


def good_result(seed, **overrides):
    result = {
        "success": True,
        "terminal": True,
        "stop_reason": "terminal",
        "actions": 10,
        "attempts": 11,
        "holds": 1,
        "invalid_actions": 0,
        "board_changes": 9,
        "policy_reobservations": 0,
        "attempt_records": [],
        "score_evaluation": {"value": 500, "confidence": 0.9},
        "max_tile": 64,
        "latency_ms": {"samples": [1.0, 2.0]},
        "resources": None,
        "wall_seconds": 100.0,
        "seed_fixture": {"seed": seed},
        "stopped_input_rejected": True,
        "stale_generation_rejected": True,
    }
    result.update(overrides)
    return result


class FakeEpisodes:
    """Each call advances the fake clock and returns the next scripted result."""

    def __init__(self, clock, script, step=100.0):
        self.clock, self.script, self.step = clock, list(script), step
        self.calls = []

    def __call__(self, provider, seed, max_steps, cache, output, **kwargs):
        self.calls.append(
            {"provider": provider, "seed": seed, "max_steps": max_steps, **kwargs}
        )
        self.clock.now += self.step
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item(seed) if callable(item) else item


class ProviderOrderTests(unittest.TestCase):
    def test_six_permutations_cycle_and_each_is_a_full_order(self):
        orders = [runner.provider_order(i) for i in range(12)]
        self.assertEqual(orders[:6], orders[6:])
        self.assertEqual(len(set(orders[:6])), 6)
        for order in orders:
            self.assertEqual(sorted(order), sorted(runner.PROVIDERS))

    def test_episode_seed_is_shared_and_wraps_uint32(self):
        self.assertEqual(runner.episode_seed(479, 1), 479)
        self.assertEqual(runner.episode_seed(479, 3), 2479)
        self.assertEqual(runner.episode_seed(0xFFFFFFFF, 2), 999)


class RunProviderTests(unittest.TestCase):
    def run_provider(self, script, *, step=100.0, provider="cycling"):
        clock = FakeClock()
        fake = FakeEpisodes(clock, script, step)
        with tempfile.TemporaryDirectory() as directory:
            report = runner.run_provider(
                identity(provider), Path(directory), Path(directory), fake, clock
            )
            on_disk = json.loads(
                (Path(directory) / "run-report.json").read_text("utf-8")
            )
        return report, fake, on_disk

    def test_all_providers_get_the_same_budget_and_episode_seeds(self):
        reports = [
            self.run_provider([lambda s: good_result(s)], provider=p)
            for p in runner.PROVIDERS
        ]
        seeds = [[c["seed"] for c in fake.calls] for _, fake, _ in reports]
        self.assertEqual(seeds[0], seeds[1])
        self.assertEqual(seeds[1], seeds[2])
        self.assertEqual(seeds[0], [479, 1479, 2479, 3479, 4479, 5479])
        for report, fake, _ in reports:
            self.assertEqual(report["budget"], runner.budget_record())
            self.assertTrue(all(c["max_steps"] == 5000 for c in fake.calls))
            self.assertTrue(all(c["deadline_monotonic"] == 600.0 for c in fake.calls))
            self.assertEqual(report["run_status"], "completed")
            self.assertEqual(report["stop_reason"], "wall_budget_reached")

    def test_new_episode_is_not_started_with_less_than_minimum_remaining(self):
        report, fake, _ = self.run_provider([lambda s: good_result(s)], step=250.0)
        # 0 -> 250 -> 500 (残り100 >= 60)で3回目。その後600超過で終了。
        self.assertEqual(len(fake.calls), 3)
        report2, fake2, _ = self.run_provider([lambda s: good_result(s)], step=545.0)
        self.assertEqual(len(fake2.calls), 1)
        self.assertAlmostEqual(report2["unused_budget_seconds"], 55.0)

    def test_recovery_exhausted_is_failed_and_counts_every_episode(self):
        bad = good_result(
            0,
            success=False,
            actions=2,
            failure_stage="decision_execution",
            failure={"type": "RuntimeError", "message": "x"},
        )
        report, fake, _ = self.run_provider([bad], step=10.0)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(
            (report["run_status"], report["stop_reason"]),
            ("failed", "recovery_exhausted"),
        )
        self.assertEqual(len(report["episodes"]), 3)

    def test_failed_then_recovered_run_is_partial_and_keeps_failed_episode(self):
        bad = good_result(0, success=False, failure_stage="initial_observation")
        report, fake, _ = self.run_provider([bad, lambda s: good_result(s)], step=200.0)
        self.assertEqual(report["run_status"], "partial")
        self.assertEqual(report["episodes"][0]["episode_status"], "failed")
        self.assertEqual(report["metrics"]["episodes"], len(report["episodes"]))

    def test_zero_input_is_never_completed(self):
        report, _, _ = self.run_provider([lambda s: good_result(s, actions=0)])
        self.assertEqual(report["run_status"], "failed")

    def test_all_attempts_held_for_the_whole_budget_is_failed_with_full_hold_rate(self):
        def held(seed):
            return good_result(
                seed, actions=0, attempts=5000, holds=5000, terminal=False
            )

        report, _, _ = self.run_provider([held], step=700.0)
        self.assertEqual(report["run_status"], "failed")
        self.assertEqual(report["metrics"]["abstain_rate"]["value"], 1.0)

    def test_stop_probe_failure_is_fatal_and_ends_the_run(self):
        probe = good_result(0, success=False, failure_stage="stop_probe")
        report, fake, _ = self.run_provider([probe, lambda s: good_result(s)])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(
            (report["run_status"], report["stop_reason"]),
            ("failed", "fatal:stop_probe"),
        )

    def test_exception_from_an_episode_is_recorded_not_lost(self):
        report, _, _ = self.run_provider(
            [RuntimeError("boom"), lambda s: good_result(s)], step=200.0
        )
        self.assertEqual(report["episodes"][0]["failure"]["type"], "RuntimeError")
        self.assertEqual(report["run_status"], "partial")

    def test_report_is_written_after_every_episode(self):
        report, _, on_disk = self.run_provider([lambda s: good_result(s)])
        self.assertEqual(on_disk["run_status"], report["run_status"])
        self.assertEqual(len(on_disk["episodes"]), len(report["episodes"]))

    def test_unreadable_score_is_unknown_in_the_report(self):
        report, _, _ = self.run_provider(
            [
                lambda s: good_result(
                    s, score_evaluation={"value": None, "confidence": 0.0}
                )
            ]
        )
        self.assertEqual(report["metrics"]["episode_mean_score"]["status"], "unknown")


class FakeProcess:
    def __init__(self, hang=False):
        self.hang, self.pid = hang, 1234

    def wait(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired("worker", timeout)
        return 0


def args_for(directory, **overrides):
    values = {
        "base_seed": 479,
        "block_index": 0,
        "ref_class": "manual",
        "duration_seconds": 600,
        "cache_dir": Path(directory),
        "output": Path(directory),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class SubprocessRunTests(unittest.TestCase):
    def worker_report(self, directory, provider, status="completed", episodes=None):
        report = {
            "schema": runner.RUN_SCHEMA,
            **identity(provider),
            "run_status": status,
            "stop_reason": "wall_budget_reached",
            "episodes": episodes if episodes is not None else [],
        }
        if status == "completed":
            episode = {
                "episode": 1,
                "seed": 479,
                "episode_status": "completed",
                "terminal": True,
                "actions": 5,
                "attempts": 5,
                "holds": 0,
                "invalid_no_op_actions": 0,
                "policy_reobservations": 0,
                "nodes": None,
                "score": 10,
                "max_tile": 8,
                "latency_ms": [1.0],
                "resources": None,
                "failure_stage": None,
                "cleanup_failure": None,
                "wall_seconds": 1.0,
            }
            report["episodes"] = [episode]
            runner.finalize_report(report, "wall_budget_reached")
        runner.write_json(Path(directory) / provider / "run-report.json", report)

    def test_hard_timeout_collects_the_process_tree_and_marks_failed(self):
        with tempfile.TemporaryDirectory() as directory:

            def spawn(command):
                self.worker_report(directory, "baseline", status="running")
                return FakeProcess(hang=True)

            with patch.object(runner, "_kill_tree", return_value=[]) as kill:
                report = runner.run_provider_subprocess(
                    args_for(directory), "baseline", identity("baseline"), spawn
                )
        kill.assert_called_once()
        self.assertEqual(
            (report["run_status"], report["stop_reason"]), ("failed", "hard_timeout")
        )

    def test_leftover_processes_after_timeout_invalidate_the_block(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(runner, "_kill_tree", return_value=[99]):
                report = runner.run_provider_subprocess(
                    args_for(directory),
                    "planner",
                    identity("planner"),
                    lambda command: FakeProcess(hang=True),
                )
            self.assertEqual(report["leftover_pids"], [99])
        runs = {
            p: {
                "condition_fingerprint": "f",
                "hardware": {},
                "budget": {},
                "episodes": [],
            }
            for p in runner.PROVIDERS
        }
        runs["planner"]["leftover_pids"] = [99]
        self.assertIn(
            "no_leftover_processes", runner.block_consistency(runs)["violations"]
        )

    def test_worker_crash_without_report_is_fatal_not_missing_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            report = runner.run_provider_subprocess(
                args_for(directory),
                "cycling",
                identity("cycling"),
                lambda command: FakeProcess(),
            )
        self.assertEqual(
            (report["run_status"], report["stop_reason"]),
            ("failed", "fatal:worker_no_report"),
        )

    def test_running_report_left_by_a_crashed_worker_is_finalized_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:

            def spawn(command):
                self.worker_report(directory, "cycling", status="running")
                return FakeProcess()

            report = runner.run_provider_subprocess(
                args_for(directory), "cycling", identity("cycling"), spawn
            )
        self.assertEqual(
            (report["run_status"], report["stop_reason"]),
            ("failed", "fatal:worker_exit"),
        )

    def test_completed_worker_report_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:

            def spawn(command):
                self.worker_report(directory, "cycling")
                return FakeProcess()

            report = runner.run_provider_subprocess(
                args_for(directory), "cycling", identity("cycling"), spawn
            )
        self.assertEqual(report["run_status"], "completed")

    def test_worker_command_never_adds_budget_or_stats_overrides(self):
        commands = []
        with tempfile.TemporaryDirectory() as directory:
            runner.run_provider_subprocess(
                args_for(directory),
                "cycling",
                identity("cycling"),
                lambda command: commands.append(command) or FakeProcess(),
            )
        self.assertIn("--worker", commands[0])
        self.assertIn("--allow-live-input", commands[0])


class BlockConsistencyTests(unittest.TestCase):
    def runs(self, **per_provider):
        base = {
            "condition_fingerprint": "f",
            "hardware": {"cpu": 1},
            "budget": {"b": 1},
            "episodes": [{"episode": 1, "seed_fixture": {"s": 1}}],
        }
        return {p: {**base, **per_provider.get(p, {})} for p in runner.PROVIDERS}

    def test_identical_runs_have_no_violation(self):
        self.assertEqual(runner.block_consistency(self.runs())["violations"], [])

    def test_hardware_fingerprint_or_budget_mismatch_is_a_violation(self):
        mismatch = runner.block_consistency(self.runs(planner={"hardware": {"cpu": 2}}))
        self.assertIn("hardware_equal", mismatch["violations"])
        mismatch = runner.block_consistency(
            self.runs(baseline={"condition_fingerprint": "g"})
        )
        self.assertIn("condition_fingerprint_equal", mismatch["violations"])
        mismatch = runner.block_consistency(self.runs(cycling={"budget": {"b": 2}}))
        self.assertIn("budget_equal", mismatch["violations"])

    def test_seed_fixture_mismatch_for_the_same_episode_is_a_violation(self):
        other = [{"episode": 1, "seed_fixture": {"s": 2}}]
        self.assertIn(
            "seed_fixture_equal",
            runner.block_consistency(self.runs(planner={"episodes": other}))[
                "violations"
            ],
        )

    def test_exit_code_is_decided_from_status_values(self):
        def block(status, **runs):
            return {
                "block_status": status,
                "runs": {
                    p: {"run_status": runs.get(p, "completed")}
                    for p in runner.PROVIDERS
                },
            }

        self.assertEqual(runner.block_exit_code(block("valid")), 0)
        self.assertEqual(runner.block_exit_code(block("valid", planner="partial")), 2)
        self.assertEqual(runner.block_exit_code(block("invalid")), 1)


class MainTests(unittest.TestCase):
    def test_live_input_must_be_explicitly_allowed_and_nothing_runs_without_it(self):
        with patch.object(runner, "run_block") as run_block:
            self.assertEqual(runner.main(["--base-seed", "479"]), 1)
        run_block.assert_not_called()

    def test_duration_cannot_exceed_the_fixed_budget(self):
        args = runner._parser().parse_args(
            ["--allow-live-input", "--duration-seconds", "601"]
        )
        self.assertIn("duration", runner._preconditions(args))

    def test_unavailable_hardware_fails_before_input(self):
        class NoRam:
            @staticmethod
            def virtual_memory():
                class Memory:
                    total = 0

                return Memory()

        with patch.dict("sys.modules", {"psutil": NoRam}):
            with self.assertRaises(runner.ConditionUnavailable):
                runner.hardware_fingerprint()


if __name__ == "__main__":
    unittest.main()
