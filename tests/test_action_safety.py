import tempfile
import unittest
from pathlib import Path

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.action_safety import (
    ActionSafetyAuditLog,
    ActionSafetyEvaluator,
    SafetyEvaluationContext,
    SafetyStatus,
)
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline


class ActionSafetyTest(unittest.TestCase):
    def setUp(self):
        self.evaluator = ActionSafetyEvaluator()
        self.observation = ScreenObservation("screen", 200, 120)

    def test_recognition_confidence_is_separate_from_safety(self):
        candidate = ActionCandidate("continue", "click", "Continue", 50, 40, 0.2, bbox=(40, 30, 30, 20))
        result = self.evaluator.evaluate(self.observation, candidate)
        self.assertEqual(result.status, SafetyStatus.SAFE)
        self.assertEqual(result.recognition_confidence, 0.2)
        self.assertGreater(result.safety_score, 0.8)
        self.assertTrue(any(item.check == "recognition_confidence" for item in result.evidence))

    def test_outside_screen_and_missing_parameters_are_blocked(self):
        outside = ActionCandidate("outside", "click", "Outside", 250, 20, 0.9)
        missing = ActionCandidate("missing", "click", "Missing", confidence=0.9)
        self.assertEqual(self.evaluator.evaluate(self.observation, outside).status, SafetyStatus.BLOCK)
        self.assertEqual(self.evaluator.evaluate(self.observation, missing).status, SafetyStatus.BLOCK)

    def test_explicit_danger_is_blocked(self):
        candidate = ActionCandidate("danger", "wait", "Danger", confidence=0.99, dangerous=True)
        result = self.evaluator.evaluate(self.observation, candidate)
        self.assertEqual(result.status, SafetyStatus.BLOCK)
        self.assertEqual(result.risk_level, "critical")
        self.assertEqual(result.safety_score, 0.0)

    def test_irreversible_action_is_suspicious_and_requests_independent_evidence(self):
        candidate = ActionCandidate("delete-save", "wait", "Delete Save Data", confidence=0.95)
        result = self.evaluator.evaluate(
            self.observation,
            candidate,
            SafetyEvaluationContext(current_goal="continue the current story"),
        )
        self.assertEqual(result.status, SafetyStatus.SUSPICIOUS)
        self.assertFalse(result.reversible)
        self.assertEqual(result.blast_radius, "high")
        self.assertEqual(result.target_scope, "game")
        self.assertFalse(result.goal_alignment)
        self.assertTrue(result.requires_verification)
        self.assertIn("confirm_irreversible_action", result.verification_requests)
        self.assertIn("verify_goal_alignment", result.verification_requests)

    def test_session_action_can_be_goal_aligned_but_remains_high_risk(self):
        candidate = ActionCandidate("quit-game", "wait", "Quit Game", confidence=0.95)
        result = self.evaluator.evaluate(
            self.observation,
            candidate,
            SafetyEvaluationContext(current_goal="quit game and end the session"),
        )
        self.assertEqual(result.status, SafetyStatus.SUSPICIOUS)
        self.assertTrue(result.goal_alignment)
        self.assertEqual(result.target_scope, "session")
        self.assertGreaterEqual(result.risk_score, 0.65)

    def test_expected_effect_conflict_requests_verification(self):
        candidate = ActionCandidate("open", "click", "Open Door", 80, 60, 0.9)
        result = self.evaluator.evaluate(
            self.observation,
            candidate,
            SafetyEvaluationContext(expected_effect_consistent=False),
        )
        self.assertEqual(result.status, SafetyStatus.SUSPICIOUS)
        self.assertFalse(result.expected_effect_consistent)
        self.assertIn("verify_expected_effect", result.verification_requests)

    def test_high_utility_high_risk_is_reported_as_upstream_anomaly(self):
        candidate = ActionCandidate("quit-game", "wait", "Quit Game", confidence=0.95)
        result = self.evaluator.evaluate(
            self.observation,
            candidate,
            SafetyEvaluationContext(current_goal="keep playing", utility_score=0.95, utility_confidence=0.9),
        )
        self.assertTrue(result.upstream_anomaly)
        self.assertTrue(any(item.check == "upstream_score_anomaly" for item in result.evidence))

    def test_audit_log_keeps_evaluation_execution_and_actual_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            log = ActionSafetyAuditLog(Path(directory) / "safety.json")
            result = self.evaluator.evaluate(self.observation, ActionCandidate("wait", "wait", "Wait", confidence=0.9))
            log.append_evaluation(result, snapshot_id="snapshot-1", goal="continue")
            log.append_execution(result.assessment_id, ExecutionResult("wait", True, "live", "ok"))
            log.append_outcome(result.assessment_id, "success", 0.8, "screen advanced")
            entries = log.entries()
            self.assertEqual([entry["event"] for entry in entries], ["evaluation", "execution", "actual_outcome"])
            self.assertTrue(all(entry["assessment_id"] == result.assessment_id for entry in entries))

    def test_live_pipeline_requires_verification_before_suspicious_input(self):
        class Source:
            def read(self):
                return ScreenObservation("menu", 200, 120), [ActionCandidate("quit-game", "wait", "Quit Game", confidence=0.9)]

        class FakeExecutor:
            def __init__(self):
                self.calls = 0

            def execute(self, candidate):
                self.calls += 1
                return ExecutionResult(candidate.action_id, True, "live", "ok")

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(Source(), Path(directory), dry_run=False)
            fake = FakeExecutor()
            pipeline.executor.live_executor = fake
            with self.assertRaisesRegex(RuntimeError, "requires verification"):
                pipeline.run_and_execute(purpose="keep playing")
            self.assertEqual(fake.calls, 0)
            entries = pipeline.safety_audit.entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["result"]["status"], "SUSPICIOUS")

    def test_safe_live_pipeline_executes_and_can_attach_outcome(self):
        class Source:
            def read(self):
                return ScreenObservation("menu", 200, 120), [ActionCandidate("continue", "wait", "Continue", confidence=0.9)]

        class FakeExecutor:
            def execute(self, candidate):
                return ExecutionResult(candidate.action_id, True, "live", "ok")

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(Source(), Path(directory), dry_run=False)
            pipeline.executor.live_executor = FakeExecutor()
            result = pipeline.run_and_execute(purpose="continue")
            pipeline.record_safety_outcome("success", 0.9, "next state observed")
            self.assertTrue(result.executed)
            entries = pipeline.safety_audit.entries()
            self.assertEqual([entry["event"] for entry in entries], ["evaluation", "execution", "actual_outcome"])
            self.assertEqual(entries[0]["result"]["status"], "SAFE")


if __name__ == "__main__":
    unittest.main()
