import unittest

from ai_game_player.bright_region_detector import BrightRegionDetector
from ai_game_player.screen_capture import ScreenFrame


class BrightRegionDetectorTest(unittest.TestCase):
    def test_detects_bright_rectangle_as_center_candidate(self):
        pixels = bytearray(6 * 5 * 4)
        for y in range(1, 4):
            for x in range(2, 5):
                offset = (y * 6 + x) * 4
                pixels[offset : offset + 4] = bytes([255, 255, 255, 255])
        candidates = BrightRegionDetector().detect(ScreenFrame(6, 5, bytes(pixels)))
        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0].x, candidates[0].y), (3, 2))
        self.assertGreaterEqual(candidates[0].confidence, 0.5)

    def test_ignores_small_bright_noise(self):
        pixels = bytearray(4 * 4 * 4)
        pixels[0:4] = bytes([255, 255, 255, 255])
        self.assertEqual(BrightRegionDetector().detect(ScreenFrame(4, 4, bytes(pixels))), [])