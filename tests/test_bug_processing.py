import tempfile
import unittest
from pathlib import Path

from ai_game_player.applications.quality.process.bug_processing import run_bug_checks


class BugProcessingTest(unittest.TestCase):
    def test_clean_source_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clean.py"
            path.write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
            report = run_bug_checks([path])
            self.assertTrue(report["passed"])
            self.assertEqual(report["findings"], [])

    def test_high_confidence_bug_patterns_are_reported(self):
        source = """
def broken(items=[]):
    return items

def duplicate():
    return 1

def duplicate():
    return 2

mapping = {"same": 1, "same": 2}

try:
    pass
except Exception:
    pass
except ValueError:
    pass

try:
    pass
finally:
    return_value = None

value = "x"
if value is "x":
    pass
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.py"
            path.write_text(source, encoding="utf-8")
            report = run_bug_checks([path])
            codes = {item["code"] for item in report["findings"]}
            self.assertFalse(report["passed"])
            self.assertTrue({"BUG101", "BUG201", "BUG103", "BUG104", "BUG105"}.issubset(codes))

    def test_return_in_finally_is_reported(self):
        source = """
def broken():
    try:
        raise RuntimeError("boom")
    finally:
        return 1
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finally_bug.py"
            path.write_text(source, encoding="utf-8")
            report = run_bug_checks([path])
            self.assertIn("BUG102", {item["code"] for item in report["findings"]})


if __name__ == "__main__":
    unittest.main()
