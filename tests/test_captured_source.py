import unittest
from ai_game_player.captured_source import CapturedObservationSource
from ai_game_player.screen_capture import ScreenFrame
class Capture:
    def capture(self): return ScreenFrame(1,1,bytes([1,2,3,255]))
class CapturedSourceTest(unittest.TestCase):
    def test_capture_is_analyzed_into_observation(self):
        observation,candidates=CapturedObservationSource(Capture(),"live").read()
        self.assertEqual(observation.screen_id,"live"); self.assertEqual(candidates,[]); self.assertEqual(observation.features["mean_rgb"]["r"],3)