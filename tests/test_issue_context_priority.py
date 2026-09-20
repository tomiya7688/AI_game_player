import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "issue_context.py"
SPEC = importlib.util.spec_from_file_location("issue_context", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def issue(number: int, *labels: str) -> dict:
    return {
        "number": number,
        "labels": [{"name": label} for label in labels],
    }


class IssueContextPriorityTest(unittest.TestCase):
    def test_priority_rank_supports_p0_through_p5(self):
        for rank in range(6):
            with self.subTest(rank=rank):
                self.assertEqual(MODULE.key(issue(100 + rank, f"P{rank}"))[0], rank)

    def test_lower_priority_number_wins_before_issue_number(self):
        candidates = [
            issue(1, "P5"),
            issue(999, "P1"),
            issue(2, "P4"),
        ]
        selected = min(candidates, key=MODULE.key)
        self.assertEqual(selected["number"], 999)

    def test_unlabeled_issue_is_after_p5(self):
        self.assertGreater(MODULE.key(issue(1))[0], MODULE.key(issue(2, "P5"))[0])


if __name__ == "__main__":
    unittest.main()
