import json
import tempfile
import unittest
from pathlib import Path
from ai_game_player.observation_source import JsonObservationSource
class ObservationSourceTest(unittest.TestCase):
    def test_reads_observation_and_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"observation.json"; path.write_text(json.dumps({"screen_id":"menu","width":100,"height":80,"candidates":[{"action_id":"start","kind":"click","label":"Start","x":10,"y":20}]}),encoding="utf-8")
            observation,candidates=JsonObservationSource(path).read()
            self.assertEqual(observation.screen_id,"menu"); self.assertEqual(candidates[0].action_id,"start")