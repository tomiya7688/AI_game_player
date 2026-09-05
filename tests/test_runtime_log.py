import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.runtime_log import RuntimeLog


class RuntimeLogTest(unittest.TestCase):
    def test_appends_json_lines_and_creates_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "runtime.jsonl"
            log = RuntimeLog(path)
            log.write("decision", "done", {"action_id": "start"})
            log.write("error", "failed")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["event"], "decision")
            self.assertEqual(first["context"]["action_id"], "start")
            self.assertTrue(first["timestamp"].endswith("+00:00"))