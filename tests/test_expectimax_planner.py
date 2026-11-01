from dataclasses import asdict, replace
from fractions import Fraction
import json
import math
import random
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from ai_game_player.belief_candidate_store import (
    BeliefCandidateStore,
    BeliefStorePolicy,
)
from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.bounded_planner import BoundedExpectimaxPlanner
from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.expectimax_search import (
    ExpectimaxSearch,
    SearchCutoff,
    SearchHypothesis,
    TraceAccumulator,
)
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.game_2048_transition import ACTION_DIRECTION_BY_ID
from ai_game_player.planning_2048 import reference_2048_registration
from ai_game_player.planning_contract import (
    PlanningDomain,
    PlanningRegistration,
    SearchBudget,
)
from ai_game_player.planning_result import WeightedValue
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_worker_limits import apply_worker_limits
from ai_game_player.planning_worker import _execute
from ai_game_player.planning_worker_diagnostic import WorkerDiagnostic
from ai_game_player.planning_wire import publish_planning_json, read_planning_json
from ai_game_player.run_control import RunController
from ai_game_player.transition_model import ActionTransition, ChanceOutcome
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)


class ToyModel:
    def __init__(self, behavior="table"):
        self.behavior = behavior
        self.calls = 0

    def apply_action(self, state, action_id):
        self.calls += 1
        if self.behavior == "slow" or (self.behavior == "slow_deep" and state >= 10):
            time.sleep(2)
        if self.behavior == "allocate":
            _ = bytearray(512 * 1024 * 1024)
        if self.behavior in ("depth", "transposition"):
            return ActionTransition(
                1 if self.behavior == "transposition" else state + 1, True, 0
            )
        return ActionTransition(
            ({"left": 10, "right": 30}[action_id] + state * 10), True, 0
        )

    def chance_outcomes(self, transition):
        if self.behavior == "split_legal":
            return (ChanceOutcome(10, 0.5), ChanceOutcome(11, 0.5))
        if self.behavior == "invalid_probability":
            return (ChanceOutcome(11, 0.8), ChanceOutcome(12, 0.8))
        if self.behavior == "huge_probability":
            return (ChanceOutcome(11, 10**500),)
        if self.behavior == "wrong_state":
            return (ChanceOutcome("wrong", 1.0),)
        if self.behavior == "oversize":
            return (ChanceOutcome(2**300, 1.0),)
        if self.behavior == "depth":
            return (ChanceOutcome(transition.state, 1),)
        if self.behavior == "transposition":
            return (
                ChanceOutcome(transition.state, 0.4),
                ChanceOutcome(transition.state, 0.6),
            )
        first = (
            0.75 if transition.state == 10 else 0.25 if transition.state == 20 else 0.5
        )
        return (
            ChanceOutcome(transition.state + 1, first),
            ChanceOutcome(transition.state + 2, 1 - first),
        )


class ToyQuality:
    def __init__(self, behavior="table"):
        self.behavior = behavior

    def evaluate(self, state):
        if self.behavior == "slow_quality":
            time.sleep(2)
        if self.behavior == "unknown_quality":
            return NumericQuality()
        value = {
            11: 0.8,
            12: -0.8,
            21: 0.2,
            22: 0.6,
            31: 0.3,
            32: 0.3,
            41: 0.8,
            42: 0.8,
        }.get(state, min(1, state / 10))
        support = 0.5 if self.behavior == "partial_quality" else 1.0
        return NumericQuality(
            value=value, confidence=support, available=True, coverage=support
        )


def decode_int(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"int"}:
        raise ValueError("expected int typed state")
    return int(value["int"])


def domain(behavior):
    def legal(state):
        if behavior == "split_legal" and state in (10, 11):
            return ("left",) if state == 10 else ("right",)
        if behavior in ("depth", "slow_deep"):
            return ("left",)
        if behavior == "intersection" and state == 1:
            return ("right",)
        return ("left", "right") if state < 10 else ()

    return PlanningDomain(
        int, decode_int, ToyModel(behavior), legal, ToyQuality(behavior)
    )


def split_legal_domain():
    return domain("split_legal")


def unexpected_factory_domain():
    raise RuntimeError("private-model-string-must-not-be-recorded")


def table_domain():
    return domain("table")


def depth_domain():
    return domain("depth")


def cache_domain():
    return domain("transposition")


def slow_domain():
    return domain("slow")


def slow_deep_domain():
    return domain("slow_deep")


def slow_quality_domain():
    return domain("slow_quality")


def allocate_domain():
    return domain("allocate")


def invalid_domain():
    return domain("invalid_probability")


def huge_probability_domain():
    return domain("huge_probability")


def wrong_state_domain():
    return domain("wrong_state")


def oversize_domain():
    return domain("oversize")


def intersection_domain():
    return domain("intersection")


def unknown_quality_domain():
    return domain("unknown_quality")


def partial_quality_domain():
    return domain("partial_quality")


def planning_result_diagnostic(result):
    """Bounded metadata only: never include state, forecasts, frames or free errors."""
    now = time.monotonic_ns()
    return json.dumps(
        {
            "outcome": result.outcome,
            "reason": result.reason,
            "reason_codes": getattr(result, "reason_codes", ()),
            "completed_depth": result.completed_depth,
            "limit_enforcement": result.limit_enforcement,
            "counters": asdict(result.counters),
            "received_before_cutoff": result.created_at_ns
            < result.compute_cutoff_at_ns,
            "fresh_at_assert": result.created_at_ns <= now < result.expires_at_ns,
            "receipt_to_compute_cutoff_ns": result.compute_cutoff_at_ns
            - result.created_at_ns,
            "receipt_to_expiry_ns": result.expires_at_ns - result.created_at_ns,
            "receipt_age_ns": now - result.created_at_ns,
            "run_generation": result.run_generation,
        },
        sort_keys=True,
        ensure_ascii=True,
    )[:2048]


class ExpectimaxPlannerTests(unittest.TestCase):
    def belief(
        self,
        *,
        states=(0, 1),
        weights=(0.25, 0.65),
        kind=BeliefWeightKind.CALIBRATED_PROBABILITY,
        ttl=3_000_000_000,
        game_id="counter",
        schema="counter.v1",
        state_type=int,
    ):
        now = time.monotonic_ns()
        context = BeliefContext(
            game_id,
            "fixture",
            "session",
            "snapshot",
            ("frame",),
            1,
            1,
            1,
            "capture",
            1,
            "expectimax-clock",
        )
        evidence = BeliefEvidenceRef(
            "observation",
            "frame",
            BeliefEvidenceKind.OBSERVATION,
            "synthetic",
            "v1",
            context,
            now,
            "frame",
        )
        candidates = tuple(
            WorldStateHypothesis(
                f"h{index}",
                TypedStateRef(game_id, schema, state_type, state),
                context,
                BeliefWeight(
                    kind,
                    weight,
                    "fixture.calibration"
                    if kind == BeliefWeightKind.CALIBRATED_PROBABILITY
                    else None,
                ),
                HypothesisOrigin.OBSERVATION_HYPOTHESIS,
                (evidence,),
                BeliefFreshness(now, now + ttl),
            )
            for index, (state, weight) in enumerate(zip(states, weights))
        )
        store = BeliefCandidateStore(
            state_type,
            clock_id=context.clock_id,
            policy=BeliefStorePolicy(max_age_ns=max(ttl, 1_000_000_000)),
        )
        return store.publish(context, candidates), store

    def registration(self, factory="table_domain"):
        return PlanningRegistration(
            factory,
            __name__,
            factory,
            "counter",
            "counter.v1",
            "toy_model",
            "1",
            "toy_quality",
            "1",
            future_state_observable=factory == "observed_domain",
        )

    def run_plan(
        self,
        *,
        factory="table_domain",
        belief=None,
        budget=None,
        allowed=("left", "right"),
        current=None,
        controller=None,
    ):
        if belief is None:
            belief, _ = self.belief()
        registration = self.registration(factory)
        planner = BoundedExpectimaxPlanner(
            (registration,), clock_id=belief.context.clock_id, controller=controller
        )
        result = planner.plan(
            belief,
            allowed,
            registration_id=factory,
            budget=budget or SearchBudget(max_depth=1, wall_time_ms=1000),
            current_belief=current,
        )
        if (
            result.outcome != PlanningOutcome.SELECT
            and planner.last_worker_diagnostic is not None
        ):
            print(
                "PLANNER_WORKER_DIAGNOSTIC "
                + json.dumps(planner.last_worker_diagnostic, sort_keys=True)[:4096],
                flush=True,
            )
        return result

    def test_worker_python_exception_has_only_fixed_metadata(self):
        registration = self.registration("unexpected_factory_domain")
        belief, _ = self.belief()
        planner = BoundedExpectimaxPlanner(
            (registration,), clock_id=belief.context.clock_id
        )
        result = planner.plan(
            belief,
            ("left",),
            registration_id=registration.registration_id,
            budget=SearchBudget(wall_time_ms=1000),
        )
        self.assertEqual(result.reason, "worker_failed")
        diagnostic = planner.last_worker_diagnostic
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic["python_exception_status"], "recorded")
        self.assertEqual(diagnostic["python_exception"]["phase"], "factory_create")
        self.assertEqual(
            diagnostic["python_exception"]["exception_category"], "runtime"
        )
        self.assertTrue(diagnostic["finished_exists"])
        self.assertFalse(diagnostic["checkpoint_exists"])
        self.assertNotIn("private-model-string", json.dumps(diagnostic))

    def test_parent_diagnostic_failure_cannot_change_selection_or_cleanup(self):
        with patch(
            "ai_game_player.bounded_planner.collect_worker_diagnostic",
            side_effect=MemoryError,
        ):
            result = self.run_plan()
        self.assertEqual(
            result.outcome, PlanningOutcome.SELECT, planning_result_diagnostic(result)
        )

    def test_preflight_fallback_clears_previous_worker_diagnostic(self):
        belief, _ = self.belief()
        planner = BoundedExpectimaxPlanner((), clock_id=belief.context.clock_id)
        planner.last_worker_diagnostic = {"previous": True}
        result = planner.plan(belief, ("left",), registration_id="unknown")
        self.assertEqual(result.reason, "transition_unavailable")
        self.assertIsNone(planner.last_worker_diagnostic)

    def test_multiple_worlds_and_chance_match_independent_fraction_oracle(self):
        result = self.run_plan()
        self.assertEqual(
            result.outcome, PlanningOutcome.SELECT, planning_result_diagnostic(result)
        )
        self.assertEqual(result.selected_action_id, "right")
        expected_left = Fraction(1, 4) * (
            Fraction(3, 4) * Fraction(4, 5) + Fraction(1, 4) * Fraction(-4, 5)
        ) + Fraction(13, 20) * (
            Fraction(1, 4) * Fraction(1, 5) + Fraction(3, 4) * Fraction(3, 5)
        )
        expected_right = Fraction(1, 4) * Fraction(3, 10) + Fraction(13, 20) * Fraction(
            4, 5
        )
        values = {item.action_id: item for item in result.action_values}
        self.assertAlmostEqual(values["left"].expected_quality, float(expected_left))
        self.assertAlmostEqual(values["right"].expected_quality, float(expected_right))
        for value in values.values():
            self.assertAlmostEqual(
                sum(item.probability for item in value.outcomes)
                + value.trace_unknown_mass,
                1,
            )
            self.assertAlmostEqual(value.lower_bound, value.expected_quality - 0.1)
            self.assertAlmostEqual(value.upper_bound, value.expected_quality + 0.1)
            self.assertEqual(len(value.hypothesis_values), 2)
        self.assertIn(
            result.limit_enforcement, ("linux_rlimit_as", "windows_job_commit")
        )
        self.assertLess(result.created_at_ns, result.compute_cutoff_at_ns)
        self.assertLess(result.created_at_ns, result.expires_at_ns)

    def test_common_legal_actions_intersect_every_world_and_verified_candidates(self):
        result = self.run_plan(factory="intersection_domain")
        self.assertEqual(
            tuple(item.action_id for item in result.action_values), ("right",)
        )
        self.assertEqual(result.selected_action_id, "right")
        self.assertEqual(result.allowed_action_ids, ("left", "right"))
        no_common = self.run_plan(factory="intersection_domain", allowed=("left",))
        self.assertEqual(no_common.outcome, PlanningOutcome.ABSTAIN)
        self.assertIsNone(no_common.selected_action_id)

    def test_transposition_cache_and_iterative_deepening_stay_within_budgets(self):
        belief, _ = self.belief(states=(0,), weights=(0.9,))
        result = self.run_plan(
            factory="cache_domain",
            belief=belief,
            budget=SearchBudget(
                max_depth=3,
                max_cache_entries=2,
                max_cache_bytes=1000,
                wall_time_ms=1000,
            ),
        )
        self.assertEqual(result.completed_depth, 3, planning_result_diagnostic(result))
        self.assertGreater(result.counters.cache_hits, 0)
        self.assertLessEqual(result.counters.peak_cache_entries, 2)
        self.assertLessEqual(result.counters.cache_bytes, 1000)
        deep = self.run_plan(
            factory="depth_domain",
            belief=belief,
            allowed=("left",),
            budget=SearchBudget(max_depth=3, wall_time_ms=1000),
        )
        self.assertEqual(deep.completed_depth, 3, planning_result_diagnostic(deep))
        self.assertAlmostEqual(deep.action_values[0].expected_quality, 0.27)

    def test_final_marker_does_not_leave_an_older_checkpoint_adopted(self):
        # Protocol ordering is deterministic; real process/time limits have separate regressions.
        belief, _ = self.belief()
        now = belief.created_at_ns
        finished_seen = []
        checkpoint_reads = []
        published = {}
        enforcement = (
            "windows_job_commit" if sys.platform == "win32" else "linux_rlimit_as"
        )

        def capture_publication(path, value, maximum):
            publish_planning_json(path, value, maximum)
            if path.name == "checkpoint.json":
                published[value["result"]["completed_depth"]] = read_planning_json(
                    path, maximum
                )

        def completed_worker(command, **kwargs):
            directory = Path(command[command.index("--memory-bytes") - 1])
            # Execute only the pure worker protocol synchronously. This fake does not test OS guards.
            with patch(
                "ai_game_player.planning_wire.publish_planning_json",
                capture_publication,
            ):
                self.assertEqual(
                    _execute(directory, enforcement, WorkerDiagnostic()), 0
                )
            self.assertEqual(sorted(published), [1, 2, 3])
            return SimpleNamespace(poll=lambda: 0)

        def read_with_old_checkpoint(path, maximum):
            value = read_planning_json(path, maximum)
            if path.name == "finished.json":
                finished_seen.append(value["reason"])
            elif path.name == "checkpoint.json":
                # An old opened inode may be read just before the final marker is observed.
                # Use the actual depth-one report, rather than relabeling deeper counters.
                if not finished_seen:
                    value = published[1]
                checkpoint_reads.append(value["result"]["completed_depth"])
            return value

        with (
            patch("time.monotonic_ns", return_value=now),
            patch(
                "ai_game_player.bounded_planner.subprocess.Popen",
                side_effect=completed_worker,
            ) as spawn,
            patch(
                "ai_game_player.bounded_planner.read_planning_json",
                read_with_old_checkpoint,
            ),
        ):
            result = self.run_plan(
                belief=belief, budget=SearchBudget(max_depth=3, wall_time_ms=1000)
            )
        spawn.assert_called_once()
        self.assertEqual(finished_seen, ["depth_budget"])
        self.assertEqual(
            result.outcome, PlanningOutcome.SELECT, planning_result_diagnostic(result)
        )
        self.assertEqual(result.completed_depth, 3, planning_result_diagnostic(result))
        self.assertEqual(checkpoint_reads, [1, 3])
        self.assertEqual(result.counters.nodes_visited, 112)
        self.assertEqual(result.reason, "completed_depth")
        self.assertEqual(result.created_at_ns, now)
        self.assertEqual(result.compute_cutoff_at_ns, now + 1_000_000_000)
        self.assertEqual(result.expires_at_ns, belief.expires_at_ns)

    def test_infeasible_common_future_sequence_keeps_completed_shallow_depth(self):
        result = self.run_plan(
            factory="split_legal_domain",
            budget=SearchBudget(max_depth=3, wall_time_ms=1000),
        )
        self.assertEqual(
            result.outcome, PlanningOutcome.SELECT, planning_result_diagnostic(result)
        )
        self.assertEqual(result.completed_depth, 1)
        self.assertEqual(result.continuation_semantics, "one_step")
        self.assertEqual(result.reason, "no_common_future_sequence")

    def test_node_budget_is_cumulative_and_rejects_incomplete_depth(self):
        registration = self.registration()
        reference = TypedStateRef("counter", registration.state_schema_id, int, 0)
        engine = ExpectimaxSearch(
            domain("table"),
            registration,
            SearchBudget(max_nodes=2, max_depth=4),
            (SearchHypothesis("h", reference, 1),),
            ("left", "right"),
            BeliefWeightKind.CALIBRATED_PROBABILITY,
            0,
        )
        with self.assertRaisesRegex(SearchCutoff, "node_budget"):
            list(engine.deepen())
        self.assertEqual(engine.counters().nodes_visited, 2)
        self.assertLessEqual(engine.domain.model.calls, 2)
        result = self.run_plan(
            budget=SearchBudget(max_nodes=2, max_depth=4, wall_time_ms=1000)
        )
        self.assertIsNone(result.selected_action_id)
        self.assertEqual(result.completed_depth, 0)

    def test_trace_truncation_retains_unknown_mass_without_changing_expected_value(
        self,
    ):
        result = self.run_plan(
            budget=SearchBudget(max_depth=1, max_trace_values=1, wall_time_ms=1000)
        )
        left = next(item for item in result.action_values if item.action_id == "left")
        self.assertAlmostEqual(left.expected_quality, 0.425)
        self.assertLessEqual(len(left.outcomes), 1)
        self.assertAlmostEqual(left.trace_unknown_mass, 0.1)
        self.assertTrue(
            any(item.lower_bound < item.upper_bound for item in left.outcomes)
        )
        self.assertAlmostEqual(
            sum(item.probability for item in left.outcomes) + left.trace_unknown_mass, 1
        )

    def test_interval_bin_preserves_rare_catastrophe_support_and_probability(self):
        trace = TraceAccumulator(1)
        trace.add((WeightedValue(1, 0.98), WeightedValue(-1, 0.02)), 1)
        values, unknown = trace.finish()
        self.assertEqual(len(values), 1)
        self.assertAlmostEqual(values[0].value, 0.96)
        self.assertEqual((values[0].lower_bound, values[0].upper_bound), (-1, 1))
        self.assertEqual(values[0].probability, 1)
        self.assertEqual(unknown, 0)

    def test_bin_merge_of_equal_points_cannot_round_above_its_support(self):
        # Captured from the ninth reference-play decision. The exact convex mean
        # of equal points is that same point for every pair of positive masses.
        value = 0.1666666666666667
        trace = TraceAccumulator(1)
        trace.add(
            (WeightedValue(value, 0.05950413223140496, 0.16666666666666666, value),), 1
        )
        trace.add((WeightedValue(value, 0.35454545454545455),), 0.08181818181818182)
        values, unknown = trace.finish()
        self.assertEqual(values[0].value, value)
        self.assertEqual(values[0].upper_bound, value)
        self.assertEqual(values[0].lower_bound, 0.16666666666666666)
        self.assertAlmostEqual(values[0].probability + unknown, 1)

    def test_bin_convex_means_match_fraction_oracle_across_extreme_masses(self):
        rng = random.Random(565)
        cases = [(-1.0, 1.0, 5e-324, 5e-324), (1.0, -1.0, 0.4, 1e-300)]
        cases.extend(
            (
                rng.uniform(-1, 1),
                rng.uniform(-1, 1),
                10 ** rng.uniform(-300, -1),
                10 ** rng.uniform(-300, -1),
            )
            for _ in range(100)
        )
        for first, second, p, q in cases:
            with self.subTest(points=(first, second), masses=(p, q)):
                trace = TraceAccumulator(1)
                trace.add((WeightedValue(first, p), WeightedValue(second, q)), 1)
                leaves, _ = trace.finish()
                oracle = float(
                    (Fraction(first) * Fraction(p) + Fraction(second) * Fraction(q))
                    / (Fraction(p) + Fraction(q))
                )
                self.assertLessEqual(abs(leaves[0].value - oracle), 8 * math.ulp(1.0))
                self.assertLessEqual(min(first, second), leaves[0].value)
                self.assertLessEqual(leaves[0].value, max(first, second))
                self.assertEqual(leaves[0].probability, math.fsum((p, q)))
                self.assertEqual(
                    (leaves[0].lower_bound, leaves[0].upper_bound),
                    (min(first, second), max(first, second)),
                )

    def test_reference_play_board_keeps_valid_deeper_checkpoint(self):
        registration = reference_2048_registration()
        state = Game2048State(((0, 2, 0, 0), (0, 0, 0, 2), (0, 0, 0, 4), (0, 0, 8, 4)))
        belief, _ = self.belief(
            states=(state,),
            weights=(1.0,),
            kind=BeliefWeightKind.RELATIVE_SCORE,
            ttl=10_000_000_000,
            game_id="2048",
            schema=registration.state_schema_id,
            state_type=Game2048State,
        )
        context = replace(belief.context, action_epoch=8)
        belief = replace(
            belief,
            context=context,
            candidates=tuple(
                replace(
                    candidate,
                    context=context,
                    evidence_refs=tuple(
                        replace(ref, context=context) for ref in candidate.evidence_refs
                    ),
                )
                for candidate in belief.candidates
            ),
        )
        planner = BoundedExpectimaxPlanner(
            (registration,), clock_id=belief.context.clock_id
        )
        result = planner.plan(
            belief,
            tuple(ACTION_DIRECTION_BY_ID),
            registration_id=registration.registration_id,
            budget=SearchBudget(
                max_depth=2,
                max_nodes=10000,
                wall_time_ms=2000,
                allow_conditional_selection=True,
            ),
        )
        if result.outcome != PlanningOutcome.SELECT:
            print(
                "PLANNER_WORKER_DIAGNOSTIC "
                + json.dumps(planner.last_worker_diagnostic, sort_keys=True)[:4096],
                flush=True,
            )
        self.assertEqual(
            result.outcome, PlanningOutcome.SELECT, planning_result_diagnostic(result)
        )
        self.assertEqual(result.completed_depth, 2, planning_result_diagnostic(result))
        self.assertEqual(result.probability_semantics, "conditional_score")
        self.assertEqual(result.residual_unknown_mass, 1)
        for action in result.action_values:
            self.assertIsNone(action.expected_quality)
            self.assertEqual(action.trace_unknown_mass, 1)
            for leaf in action.hypothesis_values[0].conditional_outcomes:
                self.assertLessEqual(leaf.lower_bound, leaf.value)
                self.assertLessEqual(leaf.value, leaf.upper_bound)

    def test_relative_hypothesis_has_only_conditional_chance_values_and_explicit_opt_in(
        self,
    ):
        belief, _ = self.belief(
            states=(0,), weights=(8.0,), kind=BeliefWeightKind.RELATIVE_SCORE
        )
        conservative = self.run_plan(belief=belief)
        self.assertEqual(conservative.outcome, PlanningOutcome.ABSTAIN)
        experiment = self.run_plan(
            belief=belief,
            budget=SearchBudget(
                max_depth=1, allow_conditional_selection=True, wall_time_ms=1000
            ),
        )
        self.assertEqual(
            experiment.outcome,
            PlanningOutcome.SELECT,
            planning_result_diagnostic(experiment),
        )
        self.assertEqual(experiment.probability_semantics, "conditional_score")
        for action in experiment.action_values:
            self.assertIsNone(action.expected_quality)
            self.assertEqual(action.trace_unknown_mass, 1)
            self.assertEqual(action.outcomes, ())
            self.assertEqual((action.lower_bound, action.upper_bound), (-1, 1))
            self.assertTrue(action.hypothesis_values[0].conditional_outcomes)
            self.assertIsNone(action.hypothesis_values[0].belief_weight)

    def test_partial_and_unknown_quality_use_intervals_not_false_probability(self):
        unknown = self.run_plan(factory="unknown_quality_domain")
        self.assertEqual(unknown.outcome, PlanningOutcome.OBSERVE_MORE)
        partial = self.run_plan(factory="partial_quality_domain")
        for action in partial.action_values:
            self.assertGreater(action.upper_bound - action.lower_bound, 0.9)
            self.assertTrue(
                any(item.lower_bound < item.upper_bound for item in action.outcomes)
            )
            self.assertAlmostEqual(
                sum(item.probability for item in action.outcomes)
                + action.trace_unknown_mass,
                1,
            )

    def test_invalid_probabilities_wrong_state_and_oversized_state_fall_back(self):
        for factory in (
            "invalid_domain",
            "huge_probability_domain",
            "wrong_state_domain",
            "oversize_domain",
        ):
            with self.subTest(factory=factory):
                result = self.run_plan(factory=factory)
                self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                self.assertEqual(result.completed_depth, 0)
                self.assertIsNone(result.selected_action_id)

    def test_unknown_model_and_source_mismatch_do_not_start_worker(self):
        belief, _ = self.belief()
        planner = BoundedExpectimaxPlanner((), clock_id=belief.context.clock_id)
        with patch("ai_game_player.bounded_planner.subprocess.Popen") as launch:
            self.assertEqual(
                planner.plan(belief, ("left",), registration_id="unknown").reason,
                "transition_unavailable",
            )
            launch.assert_not_called()
        registration = self.registration()
        planner = BoundedExpectimaxPlanner((registration,), clock_id="different-clock")
        self.assertEqual(
            planner.plan(
                belief, ("left",), registration_id=registration.registration_id
            ).reason,
            "clock_origin_mismatch",
        )

    def test_slow_model_and_evaluator_are_killed_without_parent_blocking(self):
        real_popen = subprocess.Popen
        for factory in ("slow_domain", "slow_quality_domain"):
            created = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                created.append(process)
                return process

            with (
                self.subTest(factory=factory),
                patch("ai_game_player.bounded_planner.subprocess.Popen", launch),
            ):
                started = time.monotonic()
                result = self.run_plan(
                    factory=factory, budget=SearchBudget(max_depth=1, wall_time_ms=250)
                )
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                self.assertEqual(result.completed_depth, 0)
                self.assertTrue(created)
                self.assertTrue(all(process.poll() is not None for process in created))
                self.assertIn(
                    result.limit_enforcement, ("linux_rlimit_as", "windows_job_commit")
                )

    def test_completed_depth_received_before_cutoff_survives_slow_later_depth(self):
        belief, _ = self.belief(states=(0,), weights=(0.9,))
        result = self.run_plan(
            factory="slow_deep_domain",
            belief=belief,
            allowed=("left",),
            budget=SearchBudget(max_depth=3, wall_time_ms=300),
        )
        self.assertEqual(
            result.outcome, PlanningOutcome.SELECT, planning_result_diagnostic(result)
        )
        self.assertEqual(result.completed_depth, 1)
        self.assertEqual(result.reason, "compute_budget")
        self.assertLess(result.created_at_ns, result.compute_cutoff_at_ns)
        self.assertGreater(time.monotonic_ns(), result.compute_cutoff_at_ns)
        self.assertGreater(result.expires_at_ns, time.monotonic_ns())

    def test_worker_memory_limit_handles_huge_callback_allocation(self):
        started = time.monotonic()
        result = self.run_plan(
            factory="allocate_domain",
            budget=SearchBudget(
                max_depth=1, worker_memory_bytes=64 * 1024**2, wall_time_ms=1000
            ),
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertIsNone(result.selected_action_id)
        self.assertIn(result.reason, ("worker_memory_limit", "worker_failed"))
        self.assertIn(
            result.limit_enforcement, ("linux_rlimit_as", "windows_job_commit")
        )

    def test_stop_and_rearm_reject_results_without_waiting_for_model(self):
        for rearm in (False, True):
            controller = RunController()
            started_process = threading.Event()
            created = []
            results = []
            real_popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                created.append(process)
                started_process.set()
                return process

            def run():
                results.append(
                    self.run_plan(
                        factory="slow_domain",
                        budget=SearchBudget(wall_time_ms=1000),
                        controller=controller,
                    )
                )

            with (
                self.subTest(rearm=rearm),
                patch("ai_game_player.bounded_planner.subprocess.Popen", launch),
            ):
                worker = threading.Thread(target=run)
                worker.start()
                self.assertTrue(started_process.wait(1))
                before = time.monotonic()
                if rearm:
                    controller.start()
                else:
                    controller.stop()
                self.assertLess(time.monotonic() - before, 0.1)
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0].reason, "stopped_or_rearmed")
                self.assertIsNone(results[0].selected_action_id)
                self.assertTrue(all(process.poll() is not None for process in created))

    def test_source_version_change_and_observation_expiry_discard_background_result(
        self,
    ):
        belief, _ = self.belief()
        holder = [belief]
        entered = threading.Event()
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            entered.set()
            return process

        results = []
        with patch("ai_game_player.bounded_planner.subprocess.Popen", launch):
            worker = threading.Thread(
                target=lambda: results.append(
                    self.run_plan(
                        factory="slow_domain",
                        belief=belief,
                        current=lambda: holder[0],
                        budget=SearchBudget(wall_time_ms=1000),
                    )
                )
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            holder[0] = replace(
                belief, hypothesis_set_version=belief.hypothesis_set_version + 1
            )
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].reason, "stale_belief_version")
        expiring, _ = self.belief(ttl=80_000_000)
        expired = self.run_plan(
            factory="slow_domain",
            belief=expiring,
            budget=SearchBudget(wall_time_ms=1000),
        )
        self.assertEqual(expired.reason, "stale_observation")
        self.assertIsNone(expired.selected_action_id)

    def test_invalid_current_snapshot_getter_falls_back_without_launch(self):
        with patch("ai_game_player.bounded_planner.subprocess.Popen") as launch:
            result = self.run_plan(current=lambda: object())
        self.assertEqual(result.reason, "stale_belief_version")
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        launch.assert_not_called()

    def test_resource_limit_unavailability_is_explicit(self):
        with (
            patch(
                "ai_game_player.planning_worker_limits.sys",
                SimpleNamespace(platform="darwin"),
            ),
            patch(
                "ai_game_player.planning_worker_limits.os",
                SimpleNamespace(name="posix"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                apply_worker_limits(64 * 1024**2, 100)

    def test_optional_2048_fixture_uses_same_search_and_preserves_ambiguous_worlds(
        self,
    ):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/belief/ambiguous_2048.json").read_text()
        )
        registration = reference_2048_registration()
        states = tuple(Game2048State(item["cells"]) for item in fixture["candidates"])
        weights = tuple(item["probability"] for item in fixture["candidates"])
        belief, store = self.belief(
            states=states,
            weights=weights,
            game_id="2048",
            schema=registration.state_schema_id,
            state_type=Game2048State,
        )
        planner = BoundedExpectimaxPlanner(
            (registration,), clock_id=belief.context.clock_id
        )
        result = planner.plan(
            belief,
            ("2048-move-left", "2048-move-right", "2048-move-down"),
            registration_id=registration.registration_id,
            budget=SearchBudget(max_depth=1, wall_time_ms=1000),
            current_belief=lambda: store.read(belief.context),
        )
        self.assertEqual(
            result.outcome, PlanningOutcome.SELECT, planning_result_diagnostic(result)
        )
        self.assertNotIn(
            "2048-move-left", {item.action_id for item in result.action_values}
        )
        self.assertEqual(len(result.action_values[0].hypothesis_values), 2)
        self.assertAlmostEqual(result.residual_unknown_mass, 0.1)
        self.assertGreater(result.counters.chance_branches, 4)

    def test_hidden_world_future_policy_cannot_choose_a_different_unobserved_action(
        self,
    ):
        belief, _ = self.belief(states=(0, 1), weights=(0.5, 0.5))
        budget = SearchBudget(max_depth=2, wall_time_ms=1000)
        partial = self.run_plan(
            factory="hidden_domain",
            belief=belief,
            budget=budget,
            allowed=("probe", "safe"),
        )
        observed = self.run_plan(
            factory="observed_domain",
            belief=belief,
            budget=budget,
            allowed=("probe", "safe"),
        )
        self.assertEqual(partial.continuation_semantics, "open_loop_mpc")
        self.assertEqual(observed.continuation_semantics, "state_observed_expectimax")
        self.assertEqual(partial.selected_action_id, "safe")
        self.assertEqual(observed.selected_action_id, "probe")
        open_values = {item.action_id: item for item in partial.action_values}
        full_values = {item.action_id: item for item in observed.action_values}
        oracle_open = Fraction(1, 2) * 1 + Fraction(1, 2) * -1
        self.assertAlmostEqual(
            open_values["probe"].expected_quality, float(oracle_open)
        )
        self.assertAlmostEqual(open_values["safe"].expected_quality, 0.3)
        self.assertAlmostEqual(full_values["probe"].expected_quality, 1.0)
        self.assertEqual(
            {item.expected_quality for item in open_values["probe"].hypothesis_values},
            {-1.0, 1.0},
        )
        self.assertEqual(
            {item.expected_quality for item in full_values["probe"].hypothesis_values},
            {1.0},
        )

    def test_zero_probability_world_does_not_remove_a_legal_action(self):
        belief, _ = self.belief(states=(0,), weights=(1.0,))
        positive = belief.candidates[0]
        zero = replace(
            positive,
            hypothesis_id="zero",
            state_ref=TypedStateRef("counter", "counter.v1", int, 1),
            weight=BeliefWeight(
                BeliefWeightKind.CALIBRATED_PROBABILITY,
                0.0,
                positive.weight.calibration_id,
            ),
        )
        belief = replace(belief, candidates=(zero, positive))
        result = self.run_plan(factory="intersection_domain", belief=belief)
        self.assertEqual(
            {item.action_id for item in result.action_values}, {"left", "right"}
        )
        self.assertEqual(
            {item.hypothesis_id for item in result.action_values[0].hypothesis_values},
            {positive.hypothesis_id},
        )


class HiddenWorldModel:
    def apply_action(self, state, action_id):
        if state in (0, 1):
            target = state + 10 if action_id == "probe" else 200
        elif state in (10, 11):
            correct = (state == 10 and action_id == "probe") or (
                state == 11 and action_id == "safe"
            )
            target = 100 if correct else 101
        else:
            raise ValueError("terminal state has no action")
        return ActionTransition(target, True, 0)

    def chance_outcomes(self, transition):
        return (ChanceOutcome(transition.state, 1),)


class HiddenWorldQuality:
    def evaluate(self, state):
        return NumericQuality(
            value={100: 1.0, 101: -1.0, 200: 0.3}.get(state, 0.0),
            confidence=1.0,
            available=True,
        )


def hidden_domain():
    return PlanningDomain(
        int,
        decode_int,
        HiddenWorldModel(),
        lambda state: ("probe", "safe") if state in (0, 1, 10, 11) else (),
        HiddenWorldQuality(),
    )


def observed_domain():
    return replace(hidden_domain(), future_state_observable=True)
