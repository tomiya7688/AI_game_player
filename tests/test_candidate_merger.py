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