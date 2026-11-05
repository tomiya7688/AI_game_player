import json
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ai_game_player.decision_context import CandidateDecisionContext, DecisionContext
from tools import run_noisy_provider_benchmark as benchmark

BUDGET_MS = 150.0
BASE_STEP_MS = 100.0
STEPS = 100
WARMUP_STEPS = 2
ROUNDS = 5

# cost(round_index, step_index_including_warmup) -> work units of BASE_STEP_MS
CostModel = Callable[[int, int], float]


class FakeClock:
    """Deterministic replacement for the benchmark's ``time`` module."""

    def __init__(self) -> None:
        self.now_ns = 0

    def perf_counter_ns(self) -> int:
        return self.now_ns

    def advance_ms(self, milliseconds: float) -> None:
        self.now_ns += round(milliseconds * 1_000_000)


class WorkloadEngine:
    """Lightweight Engine stand-in whose step performs a measurable amount of work.

    Each step asks the timed provider for a decision (keeping the benchmark's
    safety checks meaningful) and then performs ``work_units`` units of Core work,
    each advancing the fake clock by ``BASE_STEP_MS``.
    """

    def __init__(
        self, provider, clock: FakeClock, work_units: Callable[[int], float]
    ) -> None:
        self.provider = provider
        self.clock = clock
        self.work_units = work_units
        self.step_index = 0
        self.last_reliability_result = None

    def step(self, observation, candidates, purpose=""):
        allowed = tuple(
            candidate.action_id for candidate in candidates if not candidate.dangerous
        )
        context = DecisionContext(
            f"snapshot-{self.step_index}",
            {
                "screen_id": observation.screen_id,
                "signature": observation.features["signature"],
            },
            tuple(
                CandidateDecisionContext(
                    candidate.action_id,
                    candidate.kind,
                    candidate.label,
                    candidate.confidence,
                    candidate.action_id in allowed,
                    (),
                    0.0,
                    0.0,
                    False,
                    (),
                    (),
                )
                for candidate in candidates
            ),
            allowed,
            {"status": "unknown", "confidence": 0.0, "reason": ""},
            (),
            {"current_goal": purpose, "short_term_goal": ""},
            (),
        )
        decision = self.provider.choose_context(context)
        self.provider.assess_outcome(observation)
        units = self.work_units(self.step_index)
        whole_units = int(units)
        for _ in range(whole_units):
            self.clock.advance_ms(BASE_STEP_MS)
        self.clock.advance_ms(BASE_STEP_MS * (units - whole_units))
        self.step_index += 1
        self.last_reliability_result = SimpleNamespace(
            status=SimpleNamespace(value="ACCEPT")
        )
        return decision


class NoisyProviderBudgetJudgementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.directory = Path(temporary_directory.name)
        self.budgets_path = self.directory / "budgets.json"
        self.budgets_path.write_text(
            json.dumps(
                {
                    "provider_selection_p95_ms": 1.0,
                    "provider_outcome_p95_ms": 1.0,
                    "core_step_total_p95_ms": BUDGET_MS,
                    "core_step_non_provider_p95_ms": BUDGET_MS,
                }
            ),
            encoding="utf-8",
        )
        self.output_path = self.directory / "report.json"

    def run_with_cost(
        self, cost: CostModel, *, rounds: int = ROUNDS
    ) -> tuple[bool, dict]:
        clock = FakeClock()
        round_counter = iter(range(rounds))

        @contextmanager
        def engine_factory(provider) -> Iterator[WorkloadEngine]:
            round_index = next(round_counter)
            yield WorkloadEngine(
                provider, clock, lambda step_index: cost(round_index, step_index)
            )

        with (
            mock.patch.object(benchmark, "time", clock),
            mock.patch.object(benchmark, "run_out_of_set_fixture", return_value=True),
            mock.patch("builtins.print"),
        ):
            passed = benchmark.run_benchmark(
                STEPS,
                WARMUP_STEPS,
                178,
                1.0,
                self.budgets_path,
                self.output_path,
                rounds=rounds,
                engine_factory=engine_factory,
            )
        report = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(report["passed"], passed)
        return passed, report

    def core(self, report: dict) -> dict:
        return report["measurements"]["core_step_total_p95_ms"]

    def test_baseline_workload_passes(self):
        passed, report = self.run_with_cost(lambda round_index, step_index: 1)

        self.assertTrue(passed)
        self.assertEqual(self.core(report)["p95_ms"], BASE_STEP_MS)
        self.assertEqual(report["schema"], "noisy-provider-benchmark/v3")
        self.assertEqual(report["rounds"], ROUNDS)
        self.assertEqual(report["total_measured_steps"], STEPS * ROUNDS)
        self.assertEqual(report["selection_count"], STEPS * ROUNDS)

    def test_one_round_hit_by_a_runner_stall_does_not_fail(self):
        passed, report = self.run_with_cost(
            lambda round_index, step_index: 4 if round_index == 2 else 1
        )

        self.assertTrue(passed)
        self.assertEqual(
            self.core(report)["round_p95_ms"], [100.0, 100.0, 400.0, 100.0, 100.0]
        )
        self.assertEqual(self.core(report)["worst_round_p95_ms"], 400.0)
        self.assertEqual(self.core(report)["p95_ms"], BASE_STEP_MS)

    def test_hair_over_budget_round_like_pr_732_does_not_fail(self):
        # One round where the slowest 10% of steps land 0.66 ms over budget.
        def cost(round_index: int, step_index: int) -> float:
            return (
                150.66 / BASE_STEP_MS
                if round_index == 0 and step_index >= WARMUP_STEPS + 90
                else 1
            )

        passed, report = self.run_with_cost(cost)

        self.assertTrue(passed)
        self.assertAlmostEqual(self.core(report)["round_p95_ms"][0], 150.66)
        self.assertGreater(self.core(report)["round_p95_ms"][0], BUDGET_MS)

    def test_doubling_the_core_work_fails(self):
        passed, report = self.run_with_cost(lambda round_index, step_index: 2)

        self.assertFalse(passed)
        self.assertEqual(self.core(report)["p95_ms"], 2 * BASE_STEP_MS)
        self.assertFalse(self.core(report)["passed"])
        self.assertFalse(
            report["measurements"]["core_step_non_provider_p95_ms"]["passed"]
        )

    def test_tail_regression_in_every_round_fails(self):
        # Every 10th step does triple work: p50 is unchanged but p95 exceeds the budget.
        passed, report = self.run_with_cost(
            lambda round_index, step_index: 3 if step_index % 10 == 0 else 1
        )

        self.assertFalse(passed)
        self.assertEqual(self.core(report)["p50_ms"], BASE_STEP_MS)
        self.assertEqual(self.core(report)["p95_ms"], 3 * BASE_STEP_MS)

    def test_up_to_two_of_five_slow_rounds_are_tolerated(self):
        # Documented boundary: a minority (2 of 5) of slow rounds is treated as runner noise.
        passed, report = self.run_with_cost(
            lambda round_index, step_index: 4 if round_index in {1, 3} else 1
        )

        self.assertTrue(passed)
        self.assertEqual(self.core(report)["p95_ms"], BASE_STEP_MS)

    def test_slowdown_in_most_rounds_fails(self):
        passed, report = self.run_with_cost(
            lambda round_index, step_index: 4 if round_index in {0, 2, 4} else 1
        )

        self.assertFalse(passed)
        self.assertEqual(self.core(report)["p95_ms"], 400.0)

    def test_round_count_must_be_odd_and_at_least_three(self):
        for rounds in (0, 1, 2, 4):
            with self.subTest(rounds=rounds), self.assertRaises(ValueError):
                benchmark.run_benchmark(
                    STEPS,
                    WARMUP_STEPS,
                    178,
                    1.0,
                    self.budgets_path,
                    self.output_path,
                    rounds=rounds,
                )
        with self.assertRaises(ValueError):
            benchmark.judge_round_p95s([1.0, 2.0])
        self.assertEqual(benchmark.judge_round_p95s([5.0, 1.0, 9.0]), 5.0)

    def test_unsafe_selection_in_any_round_fails_closed_without_report(self):
        clock = FakeClock()

        class UnsafeEngine(WorkloadEngine):
            def step(self, observation, candidates, purpose=""):
                super().step(observation, candidates, purpose)
                return SimpleNamespace(action_id="dangerous-slot")

        @contextmanager
        def engine_factory(provider) -> Iterator[WorkloadEngine]:
            yield UnsafeEngine(provider, clock, lambda step_index: 1)

        with (
            mock.patch.object(benchmark, "time", clock),
            self.assertRaisesRegex(RuntimeError, "unsafe candidate"),
        ):
            benchmark.run_benchmark(
                STEPS,
                WARMUP_STEPS,
                178,
                1.0,
                self.budgets_path,
                self.output_path,
                engine_factory=engine_factory,
            )
        self.assertFalse(self.output_path.exists())

    def test_invalid_round_count_exits_with_input_error(self):
        argv = [
            "run_noisy_provider_benchmark.py",
            "--rounds",
            "4",
            "--budgets",
            str(self.budgets_path),
        ]
        with mock.patch("sys.argv", argv), mock.patch("sys.stderr"):
            self.assertEqual(benchmark.main(), 2)


if __name__ == "__main__":
    unittest.main()
