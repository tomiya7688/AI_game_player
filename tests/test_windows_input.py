import os
import unittest

from ai_game_player.action_executor import ActionExecutor
from ai_game_player.models import ActionCandidate


class WindowsInputTest(unittest.TestCase):
    def test_live_executor_is_explicit_on_non_windows(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError):
                ActionExecutor(False).execute(ActionCandidate("a", "click", "A", 1, 1))

    def test_custom_live_executor_can_be_verified_without_os_input(self):
        class Fake:
            def execute(self, candidate):
                return type("Result", (), {"action_id": candidate.action_id, "executed": True, "mode": "fake", "detail": "ok"})()
        result = ActionExecutor(False, Fake()).execute(ActionCandidate("a", "click", "A", 1, 1))
        self.assertTrue(result.executed)
        self.assertEqual(result.mode, "fake")