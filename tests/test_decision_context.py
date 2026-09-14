import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.decision_context import (
    DecisionContextBuilder,
    EvaluationFusion,
    EvaluatorEvidence,
)
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment


class FixedEvaluator:
    def __init__(self, name, score, confidence=1.0, reliability=1.0):
        self.name = name
        self.score = score
        self.confidence = confidence
        self.reliability = reliability

    def evaluate(self, observation, candidate, recent_history, previous_outcome):
        return EvaluatorEvidence(self.name, self.score, self.confidence, self.reliability, f"{self.name}-evidence")


class DecisionContextTest(unittest.TestCase):
    def observation(self, signature="state-a"):
        return ScreenObservation(
            "menu",
            320,
            200,
            ["START", "OPTIONS"],
            {
                "signature": signature,
                "perceptual_hash": "abcd",
                "mean_brightness": 120,
                "detected_elements": [{"raw": "not-for-decision-model"}],
                "secret_raw_feature": {"large": [1, 2, 3]},
            },
        )

    def candidate(self, action_id="start", label="START", confidence=0.91):
        return ActionCandidate(action_id, "wait", label, confidence=confidence)

    def test_context_is_compact_and_keeps_recognition_separate_from_utility(self):
        candidate = self.candidate()
        builder = DecisionContextBuilder(evaluators=[FixedEvaluator("utility", 0.4, 0.8, 0.75)])
        context = builder.build(
            self.observation(),
            [candidate],
            [candidate],
            previous_outcome=OutcomeAssessment("unknown", 0.2, "uncertain"),
            current_goal="clear game",
            short_term_goal="start game",
        )
        payload = context.to_dict()
        packaged = payload["candidates"][0]
        self.assertEqual(packaged["recognition_confidence"], 0.91)
        self.assertEqual(packaged["evaluation"]["score"], 0.4)
        self.assertEqual(packaged["evaluation"]["evidence"][0]["reliability"], 0.75)
        self.assertNotIn("secret_raw_feature", payload["state"])
        self.assertNotIn("detected_elements", payload["state"])
        self.assertNotIn("x", packaged)
        self.assertEqual(payload["goal"]["short_term_goal"], "start game")
        self.assertEqual(payload["schema"], "decision-context/v1")

    def test_evaluation_fusion_detects_conflict(self):
        candidate = self.candidate()
        builder = DecisionContextBuilder(
            evaluators=[FixedEvaluator("positive", 0.8), FixedEvaluator("negative", -0.7)],
            fusion=EvaluationFusion(conflict_threshold=0.3),
        )
        context = builder.build(self.observation(), [candidate], [candidate])
        packaged = context.candidates[0]
        self.assertTrue(packaged.evaluator_conflict)
        self.assertIn("evaluator_conflict", packaged.uncertainty)
        self.assertIn("candidate_evaluator_conflict", context.uncertainty)

    def test_candidate_knowledge_keeps_source_confidence_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.json"
            store = KnowledgeStore(path)
            entry = store.add("transition", "START button", "opens the game menu", 0.82)
            candidate = self.candidate()
            context = DecisionContextBuilder(store, evaluators=[FixedEvaluator("utility", 0.2)]).build(
                self.observation(), [candidate], [candidate]
            )
            evidence = context.candidates[0].knowledge[0]
            self.assertEqual(evidence.evidence_id, entry["id"])
            self.assertEqual(evidence.source, "knowledge_store")
            self.assertEqual(evidence.confidence, 0.82)
            self.assertEqual(evidence.provenance, str(path))

    def test_engine_reuses_previous_outcome_and_avoids_stalled_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory))
            candidates = [
                self.candidate("primary", "PRIMARY", 0.95),
                self.candidate("alternate", "ALTERNATE", 0.70),
            ]
            first = engine.step(self.observation("same"), candidates)
            second = engine.step(self.observation("same"), candidates)
            self.assertEqual(first.action_id, "primary")
            self.assertEqual(second.action_id, "alternate")
            trace = json.loads((Path(directory) / "decision_trace.json").read_text(encoding="utf-8"))
            self.assertEqual(len(trace), 2)
            self.assertEqual(trace[1]["previous_outcome"]["status"], "ongoing")
            self.assertFalse(trace[1]["previous_outcome"]["state_changed"])

    def test_trace_binds_snapshot_decision_evaluations_and_knowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = KnowledgeStore(root / "knowledge.json")
            evidence = store.add("strategy", "START", "safe opening action", 0.9)
            engine = GamePlayerEngine(root)
            decision = engine.step(self.observation(), [self.candidate()])
            trace = json.loads((root / "decision_trace.json").read_text(encoding="utf-8"))[0]
            self.assertEqual(trace["decision"]["action_id"], decision.action_id)
            self.assertEqual(trace["snapshot_id"], trace["context"]["snapshot_id"])
            self.assertEqual(trace["used_evidence_ids"], [evidence["id"]])
            candidate = trace["context"]["candidates"][0]
            self.assertTrue(candidate["evaluation"]["evidence"])
            self.assertEqual(candidate["knowledge"][0]["evidence_id"], evidence["id"])

    def test_fusion_uses_confidence_and_reliability_independently(self):
        score, confidence, conflict = EvaluationFusion().fuse(
            [
                EvaluatorEvidence("trusted", 1.0, 1.0, 1.0, "a"),
                EvaluatorEvidence("weak", -1.0, 0.2, 0.5, "b"),
            ]
        )
        self.assertGreater(score, 0.7)
        self.assertGreater(confidence, 0.7)
        self.assertTrue(conflict)


if __name__ == "__main__":
    unittest.main()
