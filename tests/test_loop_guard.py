import unittest
from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ScreenObservation

class LoopGuardTest(unittest.TestCase):
    def test_detects_repeated_observation(self):
        guard = LoopGuard(3)
        observation = ScreenObservation("menu", 10, 10, ["START"])
        self.assertFalse(guard.observe(observation))
        self.assertFalse(guard.observe(observation))
        self.assertTrue(guard.observe(observation))