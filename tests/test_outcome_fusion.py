import tempfile
import unittest
from pathlib import Path

from ai_game_player.engine import GamePlayerEngine
from ai_game_player.evaluation_primitives import PrimitiveEvaluator
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.outcome_fusion import OutcomeDetector


def observation(
    *,
    ocr: list[str] | None = None,
    state: dict[str, object] | None = None,
    perceptual_hash: str | None = None,
    signature: str | None = None,
) -> ScreenObservation:
    features: dict[str, object] = {}
    if state is not None:
        features["state"] = state
    if perceptual_hash is not None:
        features["perceptual_hash"] = perceptual_hash
    if signature is not None:
        features["signature"] = signature
    return ScreenObservation("screen", 100, 100, list(ocr or []), features)


class SemanticStub:
    def __init__(self, assessment: OutcomeAssessment) -> None:
        self.assessment = assessment
        self.calls = 0

    def assess_outcome(self, observation, previous=None):
        self.calls += 1
        return self.assessment


class OutcomeFusionTests(unittest.TestCase):
    def test_state_and_screen_evidence_remain_separate(self):
        detector = OutcomeDetector()
        before = observation(state={"progress": 1, "resources": 5}, perceptual_hash="0000")
        after = observation(state={"progress": 2, "resources": 4}, perceptual_hash="0000")

        event = detector.detect(before, "advance", after)

        self.assertEqual(event.status, "changed")
        by_signal = {item.signal: item for item in event.evidence}
        self.assertEqual(by_signal["state_delta"].value, "changed")
        self.assertEqual(by_signal["screen_diff"].value, "stable")
        self.assertEqual(by_signal["state_delta"].details["progress_delta"], 1.0)
        self.assertEqual(by_signal["state_delta"].details["resource_delta"], -1.0)

    def test_screen_only_animation_does_not_become_progress(self):
        detector = OutcomeDetector()
        before = observation(state={"hp": 10}, perceptual_hash="0000")
        after = observation(state={"hp": 10}, perceptual_hash="ffff")

        event = detector.detect(before, "wait", after)
        evaluation = PrimitiveEvaluator().evaluate_event(event)

        self.assertEqual(event.status, "unchanged")
        self.assertEqual(evaluation.axes["progress"].value, 0.0)
        self.assertLessEqual(evaluation.axes["progress"].confidence, 0.5)

    def test_temporal_followups_confirm_persistent_visual_change(self):
        detector = OutcomeDetector()
        before = observation(perceptual_hash="0000")
        after = observation(perceptual_hash="ffff")
        followups = (
            observation(perceptual_hash="ffff"),
            observation(perceptual_hash="fffe"),
        )

        event = detector.detect(before, "navigate", after, followups)

        state_delta = next(item for item in event.evidence if item.signal == "state_delta")
        temporal = next(item for item in event.evidence if item.signal == "temporal_change")
        self.assertEqual(state_delta.value, "unknown")
        self.assertFalse(state_delta.details["state_evidence_available"])
        self.assertEqual(temporal.value, "persistent")
        self.assertEqual(event.status, "changed")

    def test_temporal_followups_suppress_transient_visual_noise(self):
        detector = OutcomeDetector()
        before = observation(state={"hp": 10}, perceptual_hash="0000")
        after = observation(state={"hp": 10}, perceptual_hash="ffff")
        followups = (
            observation(state={"hp": 10}, perceptual_hash="0000"),
            observation(state={"hp": 10}, perceptual_hash="0001"),
        )

        event = detector.detect(before, "wait", after, followups)

        temporal = next(item for item in event.evidence if item.signal == "temporal_change")
        self.assertEqual(temporal.value, "transient")
        self.assertEqual(event.status, "unchanged")

    def test_conflicting_terminal_evidence_is_preserved(self):
        detector = OutcomeDetector()
        before = observation(ocr=["playing"])
        after = observation(ocr=["VICTORY GAME OVER"])

        event = detector.detect(before, "finish", after)

        self.assertEqual(event.status, "unknown")
        self.assertTrue(event.conflict)
        self.assertTrue(event.abstained)
        terminal_values = {item.value for item in event.evidence if item.signal == "terminal"}
        self.assertEqual(terminal_values, {"success", "failure"})

    def test_semantic_provider_is_used_only_for_low_confidence_transition(self):
        semantic = SemanticStub(OutcomeAssessment("success", 0.8, "semantic clear"))
        detector = OutcomeDetector(semantic)
        before = observation(ocr=["menu A"])
        after = observation(ocr=["menu B"])

        event = detector.detect(before, "select", after)

        self.assertEqual(semantic.calls, 1)
        self.assertTrue(event.semantic_fallback_used)
        self.assertEqual(event.status, "success")

    def test_terminal_evidence_skips_semantic_fallback(self):
        semantic = SemanticStub(OutcomeAssessment("failure", 1.0, "should not run"))
        detector = OutcomeDetector(semantic)
        before = observation(ocr=["playing"])
        after = observation(ocr=["VICTORY"])

        event = detector.detect(before, "finish", after)

        self.assertEqual(event.status, "success")
        self.assertEqual(semantic.calls, 0)
        self.assertFalse(event.semantic_fallback_used)

    def test_outcome_event_feeds_evaluation_primitives(self):
        detector = OutcomeDetector()
        before = observation(state={"progress": 2, "resources": 5}, perceptual_hash="0000")
        after = observation(state={"progress": 3, "resources": 4}, perceptual_hash="0000")

        result = PrimitiveEvaluator().evaluate_event(detector.detect(before, "advance", after))

        self.assertEqual(result.axes["progress"].value, 1.0)
        self.assertEqual(result.axes["resources"].value, -1.0)
        self.assertGreater(result.axes["novelty"].value, 0.0)

    def test_engine_tracks_previous_action_in_outcome_event(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory))
            candidate = ActionCandidate("continue", "click", "Continue", 50, 50, 0.9)
            first = observation(state={"progress": 1}, perceptual_hash="0000")
            second = observation(state={"progress": 2}, perceptual_hash="0000")

            decision = engine.step(first, [candidate])
            engine.step(second, [candidate])

            self.assertIsNotNone(engine.last_outcome_event)
            assert engine.last_outcome_event is not None
            self.assertEqual(engine.last_outcome_event.action_id, decision.action_id)
            self.assertEqual(engine.last_outcome_event.status, "changed")


if __name__ == "__main__":
    unittest.main()
