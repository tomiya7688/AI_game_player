"""Planner worker readiness gate (Issue #812): waits without relaxing the budget."""

from types import SimpleNamespace
import time
import unittest

from planning_warm_worker import warm_worker_launch

from ai_game_player.planner_2048_observation import reference_2048_readiness_belief
from ai_game_player.planner_factory import wait_for_reference_2048_planner_ready
from ai_game_player.planning_contract import SearchBudget
from ai_game_player.planning_readiness import PlannerNotReady, wait_for_planner_ready


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedPlanner:
    """Each plan() consumes `cost` seconds of the fake clock and yields a scripted depth."""

    def __init__(self, clock, depths, cost=0.4):
        self.clock, self.depths, self.cost = clock, list(depths), cost
        self.budgets = []

    def plan(self, belief, allowed, *, registration_id, budget):
        self.budgets.append(budget)
        self.clock.now += self.cost
        depth = self.depths.pop(0) if self.depths else 0
        return SimpleNamespace(
            completed_depth=depth,
            reason="compute_budget" if depth == 0 else "depth_budget",
            limit_enforcement="windows_job_commit" if depth else "not_applied",
        )


def gate(planner, clock, budget, *, deadline=130.0, **kwargs):
    return wait_for_planner_ready(
        planner,
        lambda: object(),
        ("a",),
        registration_id="r",
        budget=budget,
        deadline_monotonic=deadline,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        **kwargs,
    )


class PlanningReadinessTests(unittest.TestCase):
    budget = SearchBudget(max_depth=2, max_nodes=10000, wall_time_ms=400)

    def test_waits_through_unready_probes_then_needs_consecutive_successes(self):
        clock = FakeClock()
        planner = ScriptedPlanner(clock, [0, 0, 1, 0, 1, 2])
        report = gate(planner, clock, self.budget)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["probe_count"], 6)
        self.assertEqual(report["consecutive"], 2)
        # A failed probe after a success resets the streak (index 3 is not ready).
        self.assertEqual(
            [probe["ready"] for probe in report["probes"]],
            [False, False, True, False, True, True],
        )

    def test_budget_is_passed_through_unchanged_to_every_probe(self):
        clock = FakeClock()
        planner = ScriptedPlanner(clock, [0, 1, 1])
        report = gate(planner, clock, self.budget)
        self.assertTrue(all(item is self.budget for item in planner.budgets))
        self.assertEqual(self.budget.wall_time_ms, 400)
        self.assertEqual(report["wall_time_ms"], 400)

    def test_deadline_without_ready_fails_closed_with_bounded_diagnostic(self):
        clock = FakeClock()
        planner = ScriptedPlanner(clock, [], cost=0.4)
        with self.assertRaises(PlannerNotReady) as caught:
            gate(planner, clock, self.budget, deadline=105.0)
        error = caught.exception
        self.assertIn("last_reason=compute_budget", str(error))
        self.assertEqual(error.diagnostic["status"], "not_ready")
        self.assertLessEqual(len(error.diagnostic["probes"]), 16)
        self.assertEqual(error.diagnostic["consecutive"], 0)

    def test_no_probe_starts_when_less_than_one_budget_remains(self):
        clock = FakeClock()
        planner = ScriptedPlanner(clock, [1, 1])
        with self.assertRaises(PlannerNotReady) as caught:
            gate(planner, clock, self.budget, deadline=100.3)
        self.assertEqual(planner.budgets, [])
        self.assertIn("last_reason=no_probe", str(caught.exception))

    def test_invalid_settings_are_rejected(self):
        clock = FakeClock()
        planner = ScriptedPlanner(clock, [1])
        for kwargs in (
            {"required_consecutive": 0},
            {"required_consecutive": 6},
            {"retry_interval_s": 0.0},
            {"retry_interval_s": 3.0},
            {"deadline": float("inf")},
        ):
            with self.assertRaises(ValueError):
                gate(planner, clock, self.budget, **kwargs)
        with self.assertRaises(ValueError):
            gate(planner, clock, object())

    def test_readiness_belief_is_synthetic_and_fresh_for_the_probe(self):
        belief = reference_2048_readiness_belief(
            clock_id="python.monotonic", ttl_ns=5_400_000_000
        )
        self.assertEqual(belief.context.game_id, "2048")
        self.assertEqual(belief.context.session_id.split(".")[0], "readiness")
        self.assertEqual(belief.residual_unknown_mass, 1)
        self.assertEqual(len(belief.candidates), 1)
        self.assertLess(belief.created_at_ns, time.monotonic_ns())
        self.assertGreater(belief.expires_at_ns, time.monotonic_ns())

    def test_real_worker_round_trip_reaches_ready(self):
        # Wiring check through the real bounded worker. The interpreter start-up is moved
        # outside the budget exactly as in the planner tests (Issue #729) so this test
        # does not itself depend on the machine load the gate exists to wait out.
        budget = SearchBudget(
            max_depth=2,
            max_nodes=10000,
            wall_time_ms=2000,
            allow_conditional_selection=True,
        )
        with warm_worker_launch():
            report = wait_for_reference_2048_planner_ready(
                budget, time.monotonic() + 60.0, required_consecutive=1
            )
        self.assertEqual(report["status"], "ready")
        self.assertGreaterEqual(report["probes"][-1]["completed_depth"], 1)


if __name__ == "__main__":
    unittest.main()
