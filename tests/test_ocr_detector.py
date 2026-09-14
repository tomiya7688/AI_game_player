import unittest

from ai_game_player.models import DetectedElement, ScreenObservation
from ai_game_player.ocr_detector import OcrTextCandidateDetector


class OcrDetectorTest(unittest.TestCase):
    def test_ocr_box_becomes_detected_element(self):
        result = OcrTextCandidateDetector().detect(
            ScreenObservation("menu", 200, 100),
            [{"text": "START", "x": 10, "y": 20, "width": 60, "height": 20}],
        )
        self.assertIsInstance(result[0], DetectedElement)
        self.assertEqual(result[0].text, "START")
        self.assertEqual(result[0].bbox, (10, 20, 60, 20))
        self.assertEqual(result[0].source, "ocr")

    def test_empty_or_invalid_boxes_are_skipped(self):
        result = OcrTextCandidateDetector().detect(
            ScreenObservation("menu", 200, 100),
            [{"text": ""}, {"text": "bad", "width": 0, "height": 10}],
        )
        self.assertEqual(result, [])