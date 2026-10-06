import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.execution_history import ExecutionHistory


class ExecutionHistoryTest(unittest.TestCase):
    def test_records_and_reloads_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "execution_history.json"
            history = ExecutionHistory(path)
            expected = ExecutionResult("a", False, "dry_run", "none")

            history.append(expected)

            self.assertEqual(history.load(), [expected])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[0]["action_id"], "a")

    def test_failed_replace_preserves_previous_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution_history.json"
            original = '[{"action_id":"before","executed":false,"mode":"dry_run","detail":""}]'
            path.write_text(original, encoding="utf-8")
            history = ExecutionHistory(path)

            with patch("ai_game_player.atomic_json.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    history.append(ExecutionResult("after", True, "live", "done"))

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual([entry.action_id for entry in history.load()], ["before"])
            self.assertEqual(list(Path(directory).glob(".execution_history.json.*.tmp")), [])

    def test_malformed_json_is_reported_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution_history.json"
            malformed = "{broken"
            path.write_text(malformed, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid execution history JSON"):
                ExecutionHistory(path).append(ExecutionResult("a", False, "dry_run", "none"))

            self.assertEqual(path.read_text(encoding="utf-8"), malformed)

    def test_non_array_json_is_reported_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution_history.json"
            original = '{"action_id":"not-an-array"}'
            path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must contain an array"):
                ExecutionHistory(path).append(ExecutionResult("a", False, "dry_run", "none"))

            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
