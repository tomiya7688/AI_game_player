from dataclasses import replace
import unittest

from ai_game_player.belief_contract import BeliefWeightKind
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.decision_withheld import DecisionWithheld
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.planner_2048_observation import Reference2048PlanningObservation


class ReferencePlanningObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100_000_000
        self.adapter = Reference2048PlanningObservation(
            clock_id="clock", clock_ns=lambda: self.now
        )
        self.observation = ScreenObservation(
            "screen",
            100,
            100,
            features={
                "signature": "pixels",
                "capture_metadata": {
                    "source_id": "source",
                    "frame_id": "frame1",
                    "source_generation": 0,
                    "monotonic_ns": 10_000_000,
                },
                "game_2048": {
                    "board": [[2, 0, 0, 0], [0] * 4, [0] * 4, [0] * 4],
                    "confidence": 0.99,
                    "game_over": False,
                    "score": None,
                },
            },
        )
        candidate = ActionCandidate("2048-move-left", "key", "LEFT", confidence=0.99)
        self.context = DecisionContextBuilder().build(
            self.observation, [candidate], [candidate]
        )

    def test_screen_hypothesis_is_score_only_and_keeps_full_unknown_mass(self):
        result = self.adapter.prepare(self.context, self.observation, 0, 1, self.now)
        self.assertEqual(result.belief.weight_kind, BeliefWeightKind.RELATIVE_SCORE)
        self.assertEqual(result.belief.residual_unknown_mass, 1)
        self.assertIsNone(result.belief.candidates[0].weight.calibration_id)
        self.assertEqual(result.belief.candidates[0].state_ref.state.score, None)
        self.assertEqual(
            self.adapter.semantic_state(self.observation, 0),
            (result.semantic_digest, 0),
        )

    def test_unsupported_game_is_capability_fallback_but_missing_low_confidence_state_is_withheld(
        self,
    ):
        unsupported = replace(self.observation, features={})
        self.assertIsNone(
            self.adapter.prepare(self.context, unsupported, 0, 1, self.now)
        )
        for value in (
            None,
            {"board": None},
            dict(self.observation.features["game_2048"], confidence=0.4),
        ):
            observation = replace(
                self.observation,
                features=dict(self.observation.features, game_2048=value),
            )
            with self.assertRaises(DecisionWithheld):
                self.adapter.prepare(self.context, observation, 0, 1, self.now)

    def test_expired_capture_is_not_baseline_fallback(self):
        with self.assertRaises(DecisionWithheld):
            self.adapter.prepare(self.context, self.observation, 0, 1, 10**11)

    def test_run_generation_reconfiguration_does_not_mix_old_scope(self):
        first = self.adapter.prepare(self.context, self.observation, 0, 1, self.now)
        second = self.adapter.prepare(self.context, self.observation, 0, 2, self.now)
        self.assertNotEqual(
            first.belief.context.run_scope, second.belief.context.run_scope
        )
        self.assertGreater(
            second.belief.hypothesis_set_version, first.belief.hypothesis_set_version
        )
