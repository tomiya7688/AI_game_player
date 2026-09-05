import unittest

from ai_game_player.models import ActionCandidate, ScreenObservation


class ModelValidationTest(unittest.TestCase):
    def test_rejects_invalid_observation_size(self):
        with self.assertRaises(ValueError):
            ScreenObservation("menu", 0, 80)

    def test_rejects_invalid_confidence(self):
        with self.assertRaises(ValueError):
            ActionCandidate("a", "click", "A", confidence=1.2)

    def test_rejects_partial_coordinates(self):
        with self.assertRaises(ValueError):
            ActionCandidate("a", "click", "A", x=1)