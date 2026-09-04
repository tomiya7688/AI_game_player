import tempfile
import unittest
from pathlib import Path
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
class Source:
    def read(self): return ScreenObservation("s",100,100),[ActionCandidate("ok","wait","Wait")]
class PipelineExecuteTest(unittest.TestCase):
    def test_run_and_execute_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            result=DecisionPipeline(Source(),Path(d)).run_and_execute()
            self.assertEqual(result.action_id,"ok"); self.assertFalse(result.executed)