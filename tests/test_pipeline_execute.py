import tempfile
import unittest
from pathlib import Path

from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
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
                return ActionDecision("ocr-0", "ocr", "test")
        with tempfile.TemporaryDirectory() as directory:
            result = DecisionPipeline(Source(), Path(directory), Provider()).run_and_execute([{"text": "OCR START", "x": 10, "y": 10, "width": 20, "height": 10}])
            self.assertEqual(result.action_id, "ocr-0")

    def test_run_and_execute_reads_source_once_and_uses_same_candidates(self):
        class ChangingSource:
            def __init__(self):
                self.read_count = 0

            def read(self):
                self.read_count += 1
                if self.read_count == 1:
                    return ScreenObservation("first", 100, 80, []), [ActionCandidate("first-action", "wait", "wait")]
                return ScreenObservation("second", 100, 80, []), [ActionCandidate("second-action", "wait", "wait")]

        class Provider(RuleProvider):
            def choose(self, candidates, observation, purpose="", personality=""):
                return ActionDecision(candidates[0].action_id, "snapshot", "test")

        source = ChangingSource()
        with tempfile.TemporaryDirectory() as directory:
            result = DecisionPipeline(source, Path(directory), Provider()).run_and_execute()

        self.assertEqual(source.read_count, 1)
        self.assertEqual(result.action_id, "first-action")