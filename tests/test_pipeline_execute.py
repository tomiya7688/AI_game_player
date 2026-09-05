import tempfile
import unittest
from pathlib import Path

from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider


class Source:
    def read(self):
        return ScreenObservation("menu", 100, 80, ["OCR START"]), [ActionCandidate("configured", "wait", "wait")]


class PipelineExecuteTest(unittest.TestCase):
    def test_run_and_execute_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            result = DecisionPipeline(Source(), Path(directory)).run_and_execute()
            self.assertFalse(result.executed)
            self.assertEqual(result.mode, "dry_run")

    def test_run_and_execute_can_execute_ocr_merged_candidate(self):
        class Provider(RuleProvider):
            def choose(self, candidates, observation, purpose="", personality=""):
                return type("Decision", (), {"action_id": "ocr-0", "reason": "ocr", "provider": "test"})()
        with tempfile.TemporaryDirectory() as directory:
            result = DecisionPipeline(Source(), Path(directory), Provider()).run_and_execute([{"text": "OCR START", "x": 10, "y": 10, "width": 20, "height": 10}])
            self.assertEqual(result.action_id, "ocr-0")