import unittest

from ai_game_player.models import ScreenObservation
from ai_game_player.screen_similarity import ScreenSimilarity


class ScreenSimilarityTest(unittest.TestCase):
    def observation(self, perceptual_hash: str) -> ScreenObservation:
        return ScreenObservation("game", 100, 80, features={"perceptual_hash": perceptual_hash})

    def test_classifies_near_and_large_visual_changes(self):
        similarity = ScreenSimilarity()
        near = similarity.compare(self.observation("0000000000000000"), self.observation("0000000000000001"))
        changed = similarity.compare(self.observation("0000000000000000"), self.observation("ffffffffffffffff"))
        self.assertEqual(near["category"], "nearly_same")
        self.assertEqual(near["difference_ratio"], 1 / 64)
        self.assertEqual(changed["category"], "changed")
        self.assertEqual(changed["similarity"], 0.0)