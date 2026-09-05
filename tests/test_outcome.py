import unittest

from ai_game_player.models import ScreenObservation
from ai_game_player.outcome import OutcomeEvaluator


class OutcomeEvaluatorTest(unittest.TestCase):
    def test_detects_success_and_failure(self):
        evaluator = OutcomeEvaluator()
        self.assertEqual(evaluator.assess(ScreenObservation("s", 100, 80, ["VICTORY"])).status, "success")
        self.assertEqual(evaluator.assess(ScreenObservation("s", 100, 80, ["GAME OVER"])).status, "failure")

    def test_unknown_terminal_state_is_ongoing(self):
        result = OutcomeEvaluator().assess(ScreenObservation("s", 100, 80, ["MENU"]))
        self.assertEqual(result.status, "ongoing")
        self.assertLess(result.confidence, 0.5)