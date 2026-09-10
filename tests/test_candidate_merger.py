import unittest
from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.models import ActionCandidate
class CandidateMergerTest(unittest.TestCase):
    def test_automation_wins_nearby_ocr_duplicate(self):
        configured=ActionCandidate("shop","click","Shop",100,100,1.0)
        ocr=ActionCandidate("ocr-0","click","SHOP",110,105,.6)
        result=CandidateMerger().merge([configured],[ocr])
        self.assertEqual(result,[configured])
    def test_unrelated_candidates_are_kept(self):
        a=ActionCandidate("a","click","A",10,10); b=ActionCandidate("b","click","B",100,100)
        self.assertEqual(len(CandidateMerger().merge([a],[b])),2)
    def test_configured_and_ocr_candidates_win_over_nearby_image_candidate(self):
        configured = ActionCandidate("shop", "click", "Shop", 100, 100, 1.0)
        image = ActionCandidate("bright-region-0", "click", "bright_region", 103, 102, 0.7)
        self.assertEqual(CandidateMerger().merge([configured], [], [image]), [configured])
