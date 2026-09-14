import unittest

from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.models import ActionCandidate, DetectedElement


class CandidateMergerTest(unittest.TestCase):
    def test_automation_wins_nearby_ocr_duplicate(self):
        configured = ActionCandidate("shop", "click", "Shop", 100, 100, 1.0)
        ocr = ActionCandidate("ocr-0", "click", "SHOP", 110, 105, .6)
        self.assertEqual(CandidateMerger().merge([configured], [ocr]), [configured])

    def test_unrelated_candidates_are_kept(self):
        a = ActionCandidate("a", "click", "A", 10, 10)
        b = ActionCandidate("b", "click", "B", 100, 100)
        self.assertEqual(len(CandidateMerger().merge([a], [b])), 2)

    def test_overlapping_boxes_are_merged_beyond_coordinate_proximity(self):
        configured = ActionCandidate("configured", "click", "Configured", 50, 50, bbox=(0, 0, 100, 100))
        ocr = ActionCandidate("ocr-0", "click", "Configured", 70, 50, bbox=(20, 0, 100, 100))
        self.assertEqual(CandidateMerger(proximity=10).merge([configured], [ocr]), [configured])

    def test_configured_and_ocr_candidates_win_over_nearby_image_candidate(self):
        configured = ActionCandidate("shop", "click", "Shop", 100, 100, 1.0)
        image = ActionCandidate("bright-region-0", "click", "bright_region", 103, 102, 0.7, bbox=(90, 90, 30, 20))
        self.assertEqual(CandidateMerger().merge([configured], [], [image]), [configured])

    def test_detected_element_becomes_action_candidate_after_merge(self):
        element = DetectedElement("ocr-0", "text", (10, 20, 60, 20), "ocr", .8, "START")
        result = CandidateMerger().merge([], [element])
        self.assertEqual(result[0].action_id, "ocr-0")
        self.assertEqual(result[0].label, "START")
        self.assertEqual((result[0].x, result[0].y), (40, 30))
        self.assertEqual(result[0].confidence, .8)

    def test_overlapping_different_text_elements_are_not_collapsed(self):
        start = DetectedElement("ocr-start", "text", (0, 0, 100, 40), "ocr", .8, "START")
        settings = DetectedElement("ocr-settings", "text", (10, 0, 100, 40), "ocr", .8, "SETTINGS")
        self.assertEqual(len(CandidateMerger().merge([], [start, settings])), 2)

    def test_higher_confidence_duplicate_wins_within_same_source_group(self):
        low = DetectedElement("ocr-low", "text", (0, 0, 100, 40), "ocr", .4, "START")
        high = DetectedElement("ocr-high", "text", (0, 0, 100, 40), "ocr", .9, "start")
        result = CandidateMerger().merge([], [low, high])
        self.assertEqual([candidate.action_id for candidate in result], ["ocr-high"])
        self.assertEqual(result[0].confidence, .9)