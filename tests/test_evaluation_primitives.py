import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.evaluation_primitives import EvaluationLog, PrimitiveEvaluator
from ai_game_player.outcome import OutcomeAssessment


class EvaluationPrimitivesTest(unittest.TestCase):
    def test_terminal_outcome_maps_to_progress_and_survival(self):
        result = PrimitiveEvaluator().evaluate(OutcomeAssessment("success", 0.9, "clear detected"))

        self.assertEqual(result.axes["progress"].value, 1.0)
        self.assertEqual(result.axes["survival"].value, 1.0)
        self.assertEqual(result.axes["resources"].value, 0.0)
        self.assertGreater(result.score, 0.0)
        self.assertGreater(result.confidence, 0.0)

    def test_generic_signals_cover_all_six_axes_without_game_specific_code(self):
        result = PrimitiveEvaluator().evaluate(
            OutcomeAssessment("ongoing", 0.4, "continue"),
            {
                "progress_delta": 0.25,
                "progress_confidence": 0.8,
                "survival": 0.75,
                "survival_confidence": 0.9,
                "resource_delta": -0.5,
                "resources_confidence": 0.7,
                "novelty": 0.6,
                "novelty_confidence": 0.8,
                "repetition": 0.4,
                "repetition_confidence": 0.9,
                "irreversible_loss": 0.2,
                "irreversible_loss_confidence": 1.0,
            },
        )

        self.assertEqual(tuple(result.axes), ("progress", "survival", "resources", "novelty", "repetition", "irreversible_loss"))
        self.assertEqual(result.axes["progress"].value, 0.25)
        self.assertEqual(result.axes["survival"].value, 0.75)
        self.assertEqual(result.axes["resources"].value, -0.5)
        self.assertEqual(result.axes["novelty"].value, 0.6)
        self.assertEqual(result.axes["repetition"].value, -0.4)
        self.assertEqual(result.axes["irreversible_loss"].value, -0.2)
        self.assertEqual(result.axes["resources"].confidence, 0.7)

    def test_weights_can_be_changed_per_evaluation(self):
        evaluator = PrimitiveEvaluator({name: 0.0 for name in ("progress", "survival", "resources", "novelty", "repetition", "irreversible_loss")})
        result = evaluator.evaluate(
            OutcomeAssessment("ongoing", 0.4, "continue"),
            {"resource_delta": -0.75},
            {"resources": 1.0},
        )

        self.assertEqual(result.score, -0.75)
        self.assertEqual(result.confidence, 1.0)

    def test_decision_context_is_compact_and_structured(self):
        result = PrimitiveEvaluator().evaluate(OutcomeAssessment("failure", 0.95, "game over"))
        context = result.to_decision_context()

        self.assertEqual(context["evaluation_vector"]["progress"], -1.0)
        self.assertEqual(context["evaluation_confidence"]["progress"], 0.95)
        self.assertEqual(context["evaluation_score"], result.score)
        self.assertEqual(context["evaluation_score_confidence"], result.confidence)

    def test_evaluation_log_keeps_axes_confidence_weights_and_score(self):
        result = PrimitiveEvaluator().evaluate(OutcomeAssessment("success", 0.9, "clear"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.json"
            EvaluationLog(path).append(result, {"action_id": "start"})
            stored = json.loads(path.read_text(encoding="utf-8"))

        entry = stored[0]
        self.assertEqual(entry["metadata"]["action_id"], "start")
        self.assertIn("progress", entry["evaluation"]["axes"])
        self.assertIn("confidence", entry["evaluation"]["axes"]["progress"])
        self.assertIn("weights", entry["evaluation"])
        self.assertEqual(entry["evaluation"]["score"], result.score)

    def test_invalid_weight_and_confidence_are_rejected(self):
        with self.assertRaises(ValueError):
            PrimitiveEvaluator({"unknown": 1.0})
        with self.assertRaises(ValueError):
            PrimitiveEvaluator().evaluate(
                OutcomeAssessment("ongoing", 0.4, "continue"),
                {"novelty": 0.5, "novelty_confidence": 1.1},
            )


if __name__ == "__main__":
    unittest.main()
