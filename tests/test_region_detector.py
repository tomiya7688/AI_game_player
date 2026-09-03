import json
import tempfile
import unittest
from pathlib import Path
from ai_game_player.models import ScreenObservation
from ai_game_player.region_detector import ConfiguredRegionDetector
class RegionDetectorTest(unittest.TestCase):
    def test_named_region_becomes_center_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"automation.json"; path.write_text(json.dumps({"regions":[{"action_id":"shop","label":"Shop","x":10,"y":20,"width":40,"height":20}]}),encoding="utf-8")
            result=ConfiguredRegionDetector(path).detect(ScreenObservation("menu",100,100))
            self.assertEqual((result[0].x,result[0].y),(30,30))