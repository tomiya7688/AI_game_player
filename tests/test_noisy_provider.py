import tempfile
import unittest
from pathlib import Path

from ai_game_player.decision_context import CandidateDecisionContext, DecisionContext
from ai_game_player.decision_reliability import ReliabilityStatus
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.noisy_provider import NOISY_OUTCOME_CONFIDENCE, NoisyLanguageProvider


def decision_context(
    candidates: tuple[CandidateDecisionContext, ...],
    allowed_action_ids: tuple[str, ...],
) -> DecisionContext:
    return DecisionContext(
        "snapshot-1",
        {"screen_id": "menu", "signature": "menu-v1"},
        candidates,
        allowed_action_ids,
        {"status": "unknown", "confidence": 0.0, "reason": ""},
        (),
        {"current_goal": "", "short_term_goal": ""},
        (),
    )


def candidate_context(action_id: str, *, allowed: bool) -> CandidateDecisionContext:
    return CandidateDecisionContext(action_id, "wait", action_id, 0.9, allowed, (), 0.0, 0.0, False, (), ())


class OutOfSetProvider:
    def choose_context(self, context, personality=""):
        return ActionDecision(
            "not-allowed",
            "amber lattice follows quiet comet",
            "out_of_set_fixture",
            context.snapshot_id,
            str(context.state.get("screen_id", "")),
            str(context.state.get("signature", "")),
        )


class NoisyLanguageProviderTests(unittest.TestCase):
    def test_same_seed_repeats_action_and_word_salad_sequences(self):
        context = decision_context(
            tuple(candidate_context(f"action-{index}", allowed=True) for index in range(4)),
            tuple(f"action-{index}" for index in range(4)),
        )
        first_provider = NoisyLanguageProvider(178)
        second_provider = NoisyLanguageProvider(178)

        first = [first_provider.choose_context(context) for _ in range(20)]
        second = [second_provider.choose_context(context) for _ in range(20)]

        self.assertEqual(
            [(decision.action_id, decision.reason) for decision in first],
            [(decision.action_id, decision.reason) for decision in second],
        )
        self.assertEqual(len(first[0].reason.split()), 5)
        self.assertTrue(set(first[0].reason.split()).issubset(NoisyLanguageProvider._REASON_WORDS))

    def test_selection_stays_inside_both_allowed_candidate_sources(self):
        context = decision_context(
            (
                candidate_context("allowed-a", allowed=True),
                candidate_context("allowed-b", allowed=True),
                candidate_context("listed-but-disallowed", allowed=False),
                candidate_context("unlisted-but-marked-allowed", allowed=True),
            ),
            ("allowed-a", "allowed-b", "listed-but-disallowed"),
        )
        provider = NoisyLanguageProvider(1)

        selections = {provider.choose_context(context).action_id for _ in range(100)}

        self.assertLessEqual(selections, {"allowed-a", "allowed-b"})
        self.assertEqual(len(selections), 2)

    def test_selection_fails_closed_when_no_candidate_is_allowed(self):
        context = decision_context((candidate_context("blocked", allowed=False),), ("blocked",))

        with self.assertRaisesRegex(ValueError, "at least one allowed candidate"):
            NoisyLanguageProvider(0).choose_context(context)

    def test_seed_must_be_an_integer_but_not_a_boolean(self):
        for seed in (True, 1.5, "178"):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                NoisyLanguageProvider(seed)

    def test_semantic_outcome_is_unknown_and_low_confidence(self):
        observation = ScreenObservation("menu", 100, 80)

        assessment = NoisyLanguageProvider(178).assess_outcome(observation)

        self.assertEqual(assessment.status, "unknown")
        self.assertEqual(assessment.confidence, NOISY_OUTCOME_CONFIDENCE)
        self.assertLess(assessment.confidence, 0.5)
        self.assertTrue(assessment.reason)

    def test_core_rejects_provider_action_outside_the_allowed_set(self):
        observation = ScreenObservation("menu", 100, 80, features={"signature": "menu-v1"})
        allowed_candidate = ActionCandidate("allowed", "wait", "Allowed", confidence=0.9)
        with tempfile.TemporaryDirectory() as temporary_directory:
            engine = GamePlayerEngine(Path(temporary_directory), provider=OutOfSetProvider())

            with self.assertRaisesRegex(ValueError, "Decision rejected"):
                engine.step(observation, [allowed_candidate])

        self.assertIsNotNone(engine.last_reliability_result)
        self.assertEqual(engine.last_reliability_result.status, ReliabilityStatus.REJECT)


if __name__ == "__main__":
    unittest.main()
