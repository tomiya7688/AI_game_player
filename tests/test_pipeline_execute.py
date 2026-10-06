import tempfile
import unittest
from pathlib import Path

from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider
from ai_game_player.action_executor import ExecutionResult


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

    def test_run_and_execute_passes_the_selected_candidate_instance_to_execution(self):
        selected_candidate = ActionCandidate("selected", "wait", "Wait")
        executed_candidates = []

        class SingleCandidateSource:
            def read(self):
                return ScreenObservation("menu", 100, 80, []), [selected_candidate]

        class Provider(RuleProvider):
            def choose(self, candidates, observation, purpose="", personality=""):
                return ActionDecision(candidates[0].action_id, "select exact candidate", "test")

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(SingleCandidateSource(), Path(directory), Provider())

            def record_execution(candidate):
                executed_candidates.append(candidate)
                return ExecutionResult(candidate.action_id, False, "test", "recorded")

            pipeline.executor.execute = record_execution
            pipeline.run_and_execute()

        self.assertEqual(len(executed_candidates), 1)
        self.assertIs(executed_candidates[0], selected_candidate)

    def test_run_and_execute_never_executes_a_candidate_from_an_ambiguous_id(self):
        class DuplicateIdSource:
            def read(self):
                return ScreenObservation("menu", 100, 80, []), [
                    ActionCandidate("duplicate", "click", "Outside", 200, 10, .9),
                    ActionCandidate("duplicate", "wait", "Wait", confidence=.9),
                ]

        executed_candidates = []
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(DuplicateIdSource(), Path(directory))

            def record_execution(candidate):
                executed_candidates.append(candidate)
                return ExecutionResult(candidate.action_id, False, "test", "recorded")

            pipeline.executor.execute = record_execution
            with self.assertRaises(ValueError):
                pipeline.run_and_execute()

        self.assertEqual(executed_candidates, [])
