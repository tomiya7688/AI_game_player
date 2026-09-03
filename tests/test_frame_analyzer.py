import unittest
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.screen_capture import ScreenFrame
class FrameAnalyzerTest(unittest.TestCase):
    def test_extracts_color_and_signature(self):
        frame=ScreenFrame(2,1,bytes([10,20,30,255,10,20,30,255]))
        observation=FrameAnalyzer().analyze(frame,"test")
        self.assertEqual(observation.features["mean_rgb"],{"r":30,"g":20,"b":10})
        self.assertEqual(len(observation.features["signature"]),64)
    def test_rejects_invalid_buffer(self):
        with self.assertRaises(ValueError): FrameAnalyzer().analyze(ScreenFrame(1,1,b"bad"))