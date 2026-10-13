import tempfile
import unittest
from pathlib import Path

from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from tools.run_windows_e2e import match_sample_control_to_image


class Source:
    def read(self): return ScreenObservation("menu",200,100),[ActionCandidate("configured","click","Configured",10,10,.8)]
class PipelineTest(unittest.TestCase):
    def test_runs_source_to_decision(self):
        with tempfile.TemporaryDirectory() as d:
            decision=DecisionPipeline(Source(),Path(d)).run([{"text":"Other","x":100,"y":20,"width":20,"height":10}],"start","careful")
            self.assertEqual(decision.action_id,"configured")
            self.assertTrue((Path(d)/"history.json").exists())
    def test_rejects_image_candidate_without_ui_evidence(self):
        class ImageSource:
            def read(self):
                image_candidates = [
                    {
                        "action_id": "bright",
                        "kind": "click",
                        "label": "bright_region",
                        "x": 20,
                        "y": 20,
                        "confidence": 0.0,
                        "bbox": [10, 10, 20, 20],
                    }
                ]
                observation = ScreenObservation(
                    "menu",
                    200,
                    100,
                    features={"image_candidates": image_candidates},
                )
                return observation, []
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "許可された操作候補がありません"):
                DecisionPipeline(ImageSource(), Path(directory)).run()

    def test_sample_game_metadata_is_required_to_promote_bright_region(self):
        image_candidates = [
            {
                "action_id": "bright-region-0",
                "kind": "click",
                "label": "region",
                "x": 170,
                "y": 130,
                "confidence": 0.0,
                "bbox": [60, 90, 220, 80],
            }
        ]
        active_control = {
            "action_id": "advance-1",
            "label": "ADVANCE 1/24",
            "bbox_size": (220, 80),
        }

        self.assertIsNone(match_sample_control_to_image(None, image_candidates))
        candidate = match_sample_control_to_image(active_control, image_candidates)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.action_id, "advance-1")
        self.assertEqual(candidate.confidence, 1.0)
        self.assertEqual((candidate.x, candidate.y), (170, 130))
        self.assertIsNone(match_sample_control_to_image(active_control, image_candidates * 2))
