import json,tempfile,unittest
from pathlib import Path
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.models import ActionCandidate,ScreenObservation
class CoreTest(unittest.TestCase):
 def test_unsafe_excluded_and_history_saved(self):
  with tempfile.TemporaryDirectory() as folder:
   d=GamePlayerEngine(Path(folder)).step(ScreenObservation("title",640,480),[ActionCandidate("bad","click","bad",900,1),ActionCandidate("ok","click","OK",10,10,.8)])
   self.assertEqual(d.action_id,"ok"); h=json.loads((Path(folder)/"history.json").read_text(encoding="utf-8")); self.assertEqual(h[0]["decision"]["action_id"],"ok")
