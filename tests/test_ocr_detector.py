import unittest
from ai_game_player.models import ScreenObservation
from ai_game_player.ocr_detector import OcrTextCandidateDetector
class OcrDetectorTest(unittest.TestCase):
    def test_ocr_box_becomes_candidate_center(self):
        result=OcrTextCandidateDetector().detect(ScreenObservation("menu",200,100),[{"text":"START","x":10,"y":20,"width":60,"height":20}])
        self.assertEqual(result[0].label,"START"); self.assertEqual((result[0].x,result[0].y),(40,30))
    def test_empty_or_invalid_boxes_are_skipped(self):
        result=OcrTextCandidateDetector().detect(ScreenObservation("menu",200,100),[{"text":""},{"text":"bad","width":0,"height":10}])
        self.assertEqual(result,[])