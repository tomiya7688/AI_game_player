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

    def test_detects_nearly_same_perceptual_hashes(self):
        guard = LoopGuard(3)
        first = ScreenObservation("menu", 10, 10, features={"perceptual_hash": "0000000000000000"})
        near = ScreenObservation("menu", 10, 10, features={"perceptual_hash": "0000000000000001"})
        self.assertFalse(guard.observe(first))
        self.assertFalse(guard.observe(near))
        self.assertTrue(guard.observe(first))