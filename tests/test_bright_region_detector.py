import unittest

from ai_game_player.bright_region_detector import BrightRegionDetector
from ai_game_player.models import DetectedElement
from ai_game_player.screen_capture import ScreenFrame


class BrightRegionDetectorTest(unittest.TestCase):
    def test_detects_bright_rectangle_as_detected_element(self):
        pixels = bytearray(6 * 5 * 4)
        for y in range(1, 4):
            for x in range(2, 5):
                offset = (y * 6 + x) * 4
                pixels[offset : offset + 4] = bytes([255, 255, 255, 255])
        elements = BrightRegionDetector().detect(ScreenFrame(6, 5, bytes(pixels)))
        self.assertEqual(len(elements), 1)
        self.assertIsInstance(elements[0], DetectedElement)
        self.assertEqual(elements[0].bbox, (2, 1, 3, 3))
        self.assertEqual(elements[0].source, "bright_region")
        self.assertGreaterEqual(elements[0].confidence, 0.5)

    def test_ignores_small_bright_noise(self):
        pixels = bytearray(4 * 4 * 4)
        pixels[0:4] = bytes([255, 255, 255, 255])
        self.assertEqual(BrightRegionDetector().detect(ScreenFrame(4, 4, bytes(pixels))), [])