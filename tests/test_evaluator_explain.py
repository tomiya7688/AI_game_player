import unittest

from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.models import ActionCandidate, ScreenObservation


class EvaluatorExplainTest(unittest.TestCase):
    def test_reports_acceptance_and_rejection_reason(self):
        observation = ScreenObservation("menu", 100, 80)
        candidates = [
            ActionCandidate("ok", "click", "OK", 10, 10, .9),
            ActionCandidate("bad", "click", "Bad", 200, 10, .9),
            ActionCandidate("wait", "wait", "Wait", confidence=.4),
        ]
        report = ActionEvaluator().explain(observation, candidates)
        self.assertEqual(report[0]["reason"], "accepted")
        self.assertEqual(report[1]["reason"], "outside_screen")
        self.assertEqual(report[2]["reason"], "low_confidence")