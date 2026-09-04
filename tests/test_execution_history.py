import json
import tempfile
import unittest
from pathlib import Path
from ai_game_player.action_executor import ExecutionResult
from ai_game_player.execution_history import ExecutionHistory
class ExecutionHistoryTest(unittest.TestCase):
    def test_records_result(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"execution_history.json"; ExecutionHistory(path).append(ExecutionResult("a",False,"dry_run","none")); value=json.loads(path.read_text(encoding="utf-8")); self.assertEqual(value[0]["action_id"],"a")