import tempfile
import unittest
from pathlib import Path

from ai_game_player.baseline_provider import (
    ABSTAIN,
    OBSERVE_MORE,
    SELECT,
    BaselineDecisionProvider,
    BaselineProviderProfile,
    BaselineWithheld,
    baseline_capability_matrix,
)
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.models import ActionCandidate, ScreenObservation


def build_context(candidates, allowed=None):
    observation = ScreenObservation("menu", 100, 80, features={"signature": "menu-v1"})
    return observation, DecisionContextBuilder().build(observation, candidates, candidates if allowed is None else allowed)


class BaselineDecisionProviderTest(unittest.TestCase):
    def test_selects_highest_scored_allowed_candidate_with_trace(self):
        _, context = build_context(
            [ActionCandidate("a", "wait", "A", confidence=0.6), ActionCandidate("b", "wait", "B", confidence=0.95)]
        )
        decision = BaselineDecisionProvider().decide(context)
        self.assertEqual((decision.kind, decision.selected_action_id), (SELECT, "b"))
        self.assertEqual(decision.rule_id, "R3_highest_weighted_score")
        self.assertEqual(set(decision.contributions["b"]), {"utility", "recognition", "knowledge_prior"})
        self.assertGreater(decision.margin, 0)

    def test_tie_abstains_and_low_recognition_observes_more(self):
        _, tie = build_context([ActionCandidate("a", "wait", "A", confidence=0.9), ActionCandidate("b", "wait", "B", confidence=0.9)])
        self.assertEqual(BaselineDecisionProvider().decide(tie).kind, ABSTAIN)
        self.assertEqual(BaselineDecisionProvider(min_margin=0).decide(tie).selected_action_id, "a")
        _, weak = build_context([ActionCandidate("a", "wait", "A", confidence=0.2)])
        self.assertEqual(BaselineDecisionProvider().decide(weak).kind, OBSERVE_MORE)
        with self.assertRaises(BaselineWithheld):
            BaselineDecisionProvider().choose_context(weak)

    def test_never_selects_outside_allowed_actions(self):
        a = ActionCandidate("a", "wait", "A", confidence=0.99)
        b = ActionCandidate("b", "wait", "B", confidence=0.6)
        _, context = build_context([a, b], [b])
        self.assertEqual(BaselineDecisionProvider(min_margin=0).decide(context).selected_action_id, "b")
        _, none = build_context([a], [])
        with self.assertRaises(ValueError):
            BaselineDecisionProvider().decide(none)

    def test_knowledge_snapshot_prior_changes_choice_and_broken_snapshot_falls_back(self):
        candidates = [ActionCandidate("a", "wait", "A", confidence=0.9), ActionCandidate("b", "wait", "B", confidence=0.9)]
        _, context = build_context(candidates)
        good = BaselineDecisionProvider(knowledge_snapshot={"version": "k1", "priors": {"b": 0.8}}).decide(context)
        self.assertEqual((good.selected_action_id, good.knowledge_snapshot_version), ("b", "k1"))
        broken = BaselineDecisionProvider(knowledge_snapshot={"version": "k1", "priors": {"b": 9}}).decide(context)
        self.assertEqual(broken.kind, ABSTAIN)
        self.assertIsNone(broken.knowledge_snapshot_version)
        self.assertIn("knowledge_snapshot_rejected", broken.provenance[0])

    def test_profile_is_machine_readable_and_rejects_model_requirements(self):
        profile = BaselineDecisionProvider().profile.to_dict()
        self.assertFalse(profile["requires_learned_model"] or profile["requires_gpu"] or profile["requires_network"])
        with self.assertRaises(ValueError):
            BaselineProviderProfile("x", "quantitative", (), requires_gpu=True)
        matrix = baseline_capability_matrix([BaselineDecisionProvider().profile], ("candidate_choice", "binary_gate"))
        self.assertEqual(matrix, {"candidate_choice": "baseline_weighted_score", "binary_gate": None})

    def test_model_less_core_path_runs_through_engine_and_verifier(self):
        observation = ScreenObservation("menu", 100, 80, features={"signature": "menu-v1"})
        candidates = [ActionCandidate("start", "wait", "Start", confidence=0.95), ActionCandidate("quit", "wait", "Quit", confidence=0.6)]
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory), provider=BaselineDecisionProvider())
            decision = engine.step(observation, candidates)
            self.assertEqual(decision.action_id, "start")
            self.assertTrue(decision.provider.startswith("baseline_weighted_score/"))
            weak = [ActionCandidate("start", "wait", "Start", confidence=0.55)]
            with self.assertRaises(BaselineWithheld):
                engine.step(observation, weak)


if __name__ == "__main__":
    unittest.main()
