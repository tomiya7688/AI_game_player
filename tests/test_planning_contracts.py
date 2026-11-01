from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.planning_contract import PlanningRegistration, SearchBudget
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_result import (
    HypothesisActionValue,
    PlanningActionValue,
    PlanningCounters,
    PlanningResult,
    WeightedValue,
)
from ai_game_player.planning_wire import (
    publish_planning_json,
    read_planning_json,
    result_from_dict,
    result_to_dict,
)


class PlanningContractTests(unittest.TestCase):
    def result(self):
        context = BeliefContext(
            "game",
            "scene",
            "session",
            "snapshot",
            ("frame",),
            1,
            1,
            1,
            "capture",
            1,
            "clock",
        )
        first = HypothesisActionValue(
            "a", 0.6, None, 0.2, 0.2, 0.2, (WeightedValue(0.2, 1),), 0
        )
        second = HypothesisActionValue(
            "b", 0.3, None, 0.8, 0.8, 0.8, (WeightedValue(0.8, 1),), 0
        )
        action = PlanningActionValue(
            "left",
            0.36,
            0.26,
            0.46,
            (WeightedValue(0.2, 0.6), WeightedValue(0.8, 0.3)),
            0.1,
            (first, second),
            0.36,
        )
        return PlanningResult(
            PlanningOutcome.SELECT,
            "left",
            (action,),
            context,
            1,
            ("left", "right"),
            "calibrated",
            0.1,
            1,
            "completed_depth",
            "model",
            "v1",
            "quality",
            "v1",
            1,
            "default.1",
            "normalized_quality.v1",
            1000,
            5000,
            2000,
            PlanningCounters(),
            "linux_rlimit_as",
            0,
        )

    def test_typed_result_and_atomic_bounded_json_roundtrip(self):
        result = self.result()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            publish_planning_json(path, result_to_dict(result), 65536)
            self.assertEqual(result_from_dict(read_planning_json(path, 65536)), result)
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                publish_planning_json(path, {"large": "x" * 8192}, 4096)
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(result.outcome, "select")

    def test_partial_quality_bounds_do_not_become_world_probabilities(self):
        quality = NumericQuality(
            value=0.8,
            confidence=0.5,
            available=True,
            coverage=0.5,
            missing_axes=("risk",),
        )
        leaf = WeightedValue(quality.effective_value, 1.0, *quality.bounds)
        self.assertEqual(leaf.probability, 1.0)
        self.assertAlmostEqual(leaf.lower_bound, -0.1)
        self.assertAlmostEqual(leaf.upper_bound, 0.9)
        unknown = NumericQuality()
        self.assertEqual(WeightedValue(0, 1, *unknown.bounds).lower_bound, -1.0)

    def test_invalid_mass_interval_numbers_and_budget_are_rejected(self):
        for args in (
            (float("nan"), 1),
            (0, -0.1),
            (0, 1.1),
            (0, True),
            (0.5, 1, 0.6, 0.7),
        ):
            with self.assertRaises(ValueError):
                WeightedValue(*args)
        for field in ("value", "probability", "lower_bound", "upper_bound"):
            malformed = {
                "value": 0.2,
                "probability": 1.0,
                "lower_bound": 0.2,
                "upper_bound": 0.2,
                field: 10**500,
            }
            with self.subTest(huge_integer=field), self.assertRaises(ValueError):
                WeightedValue(**malformed)
        result = self.result()
        for changes in (
            {"selected_action_id": "unverified"},
            {"belief_version": True},
            {"completed_depth": 0},
            {"expires_at_ns": 999},
            {"allowed_action_ids": ("left", "left")},
            {"residual_unknown_mass": 0.2},
            {"probability_semantics": "conditional_score"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(result, **changes)
        for changes in (
            {"max_nodes": True},
            {"max_depth": 17},
            {"worker_memory_bytes": 1},
            {"max_trace_values": 257},
            {"allow_conditional_selection": 1},
            {"max_unknown_mass": float("nan")},
        ):
            with self.assertRaises(ValueError):
                SearchBudget(**changes)
        with self.assertRaises(ValueError):
            PlanningRegistration(
                "game",
                "../unsafe",
                "factory",
                "game",
                "v1",
                "model",
                "v1",
                "quality",
                "v1",
            )

    def test_uncalibrated_global_value_stays_unknown_but_conditional_chance_is_preserved(
        self,
    ):
        result = self.result()
        conditional = HypothesisActionValue(
            "a", None, 8.0, 0.2, 0.2, 0.2, (WeightedValue(0.2, 1),), 0
        )
        action = PlanningActionValue("left", None, -1, 1, (), 1, (conditional,), 0.2)
        experiment = replace(
            result,
            action_values=(action,),
            probability_semantics="conditional_score",
            residual_unknown_mass=1.0,
        )
        self.assertIsNone(experiment.action_values[0].expected_quality)
        self.assertEqual(experiment.action_values[0].trace_unknown_mass, 1)
        self.assertEqual(
            experiment.action_values[0]
            .hypothesis_values[0]
            .conditional_outcomes[0]
            .probability,
            1,
        )
        with self.assertRaises(ValueError):
            replace(
                experiment,
                action_values=(
                    replace(
                        action, outcomes=(WeightedValue(0.2, 1),), trace_unknown_mass=0
                    ),
                ),
            )

    def test_wire_rejects_unknown_fields_duplicate_keys_and_oversized_messages(self):
        result = result_to_dict(self.result())
        with self.assertRaises(ValueError):
            result_from_dict({**result, "unverified_action": "right"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.json"
            for raw in (
                b'{"x":1,"x":2}',
                b'{"x":NaN}',
                b"[]",
                b"x" * 4097,
                b'{"x":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}",
            ):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    read_planning_json(path, 4096)
            # Brackets and escaped quotes inside strings are not container depth.
            text = '[\\"' * 100
            publish_planning_json(path, {"text": text}, 4096)
            self.assertEqual(read_planning_json(path, 4096), {"text": text})
