import unittest
from ai_game_player.execution_mode import execution_labels

class ExecutionModeTest(unittest.TestCase):
    def test_live_and_dry_run_labels_are_distinct(self):
        self.assertIn("dry-run", execution_labels(False)[0])
        self.assertIn("実入力", execution_labels(True)[0])
        self.assertEqual(execution_labels(False)[2], "実入力: 無効")