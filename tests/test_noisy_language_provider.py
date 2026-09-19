import tempfile
import unittest
from pathlib import Path

from ai_game_player.decision_context import DecisionContext
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.testing.noisy_language_provider import (
    NoisyLanguageProvider,
    OutOfSetNoisyLanguageProvider,
)


def context_with_actions(*action_ids: str) -> DecisionContext:
    return DecisionContext(
        "snapshot",
        {"screen_id": "screen"},
        (),
        tuple(action_ids),
        {"status": "unknown", "confidence": 0.0},
        (),
        {"current_goal": "", "short_term_goal": ""},
        (),
    )


class NoisyLanguageProviderTest(unittest.TestCase):
    def test_seed_makes_decision_sequence_reproducible(self):
        left = NoisyLanguageProvider(seed=123)
        right = NoisyLanguageProvider(seed=123)
        context = context_with_actions("a", "b", "c", "d")

        left_sequence = [left.choose_context(context).action_id for _ in range(20)]
        right_sequence = [right.choose_context(context).action_id for _ in range(20)]

        self.assertEqual(left_sequence, right_sequence)
        self.assertGreater(len(set(left_sequence)), 1)

    def test_reason_is_word_salad_but_action_is_grounded(self):
        provider = NoisyLanguageProvider(seed=7, words_per_reason=8)
        decision = provider.choose_context(context_with_actions("left", "right"))

        self.assertIn(decision.action_id, {"left", "right"})
        self.assertEqual(len(decision.reason.split()), 8)
        self.assertEqual(decision.provider, "ci:noisy-language-provider")

    def test_out_of_set_provider_is_rejected_by_engine(self):
        observation = ScreenObservation("screen", 100, 100, [])
        candidates = [
            ActionCandidate("safe-a", "wait", "safe a"),
            ActionCandidate("safe-b", "wait", "safe b"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(
                Path(directory),
                provider=OutOfSetNoisyLanguageProvider(seed=5),
            )
            with self.assertRaisesRegex(
                ValueError,
                "outside the allowed snapshot",
            ):
                engine.step(observation, candidates)

    def test_semantic_outcome_remains_low_confidence(self):
        provider = NoisyLanguageProvider(seed=99)
        observation = ScreenObservation("screen", 100, 100, [])
        for _ in range(20):
            assessment = provider.assess_outcome(observation)
            self.assertIn(assessment.status, {"unknown", "ongoing", "failure", "success"})
            self.assertLessEqual(assessment.confidence, 0.25)


if __name__ == "__main__":
    unittest.main()
