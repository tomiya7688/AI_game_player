import unittest
from ai_game_player.action_executor import ActionExecutor
from ai_game_player.models import ActionCandidate
class ActionExecutorTest(unittest.TestCase):
    def test_default_is_dry_run(self):
        result=ActionExecutor().execute(ActionCandidate("start","click","Start",1,1))
        self.assertFalse(result.executed); self.assertEqual(result.mode,"dry_run")
    def test_live_mode_is_not_silently_implemented(self):
        with self.assertRaises(RuntimeError): ActionExecutor(False).execute(ActionCandidate("start","click","Start"))