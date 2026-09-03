import json
import tempfile
import unittest
from pathlib import Path
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.observation_source import ObservationSource
from ai_game_player.pipeline import DecisionPipeline
class Source:
    def read(self): return ScreenObservation("menu",200,100),[ActionCandidate("configured","click","Configured",10,10,.8)]
class PipelineTest(unittest.TestCase):
    def test_runs_source_to_decision(self):
        with tempfile.TemporaryDirectory() as d:
            decision=DecisionPipeline(Source(),Path(d)).run([{"text":"Other","x":100,"y":20,"width":20,"height":10}],"start","careful")
            self.assertEqual(decision.action_id,"configured")
            self.assertTrue((Path(d)/"history.json").exists())