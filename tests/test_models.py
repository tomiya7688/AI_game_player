import unittest

from ai_game_player.models import ActionCandidate, DetectedElement


class ModelDeserializationTest(unittest.TestCase):
    def test_detected_element_bbox_is_a_fixed_four_tuple(self):
        element = DetectedElement.from_dict(
            {"element_id": "button", "bbox": [1, 2, 3, 4]}
        )
        self.assertEqual(element.bbox, (1, 2, 3, 4))

    def test_action_candidate_rejects_malformed_bbox(self):
        with self.assertRaisesRegex(ValueError, "exactly four"):
            ActionCandidate.from_dict(
                {"action_id": "button", "kind": "click", "bbox": [1, 2, 3]}
            )

    def test_detected_element_rejects_malformed_bbox(self):
        with self.assertRaisesRegex(ValueError, "exactly four"):
            DetectedElement.from_dict({"element_id": "button", "bbox": [1, 2, 3]})