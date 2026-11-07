import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "issue_context.py"
SPEC = importlib.util.spec_from_file_location("issue_context", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def issue(number: int, *labels: str, title: str = "Implementation Issue") -> dict:
    return {
        "number": number,
        "title": title,
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

    def test_parent_issue_is_not_work_issue(self):
        self.assertIsNone(
            MODULE.select_issue(
                [issue(10, "P1", title="[Architecture Parent] Runtime")]
            )
        )

    def test_policy_issue_19_is_not_work_issue(self):
        self.assertIsNone(MODULE.select_issue([issue(19, "P1")]))

    def test_normal_issue_is_work_issue(self):
        self.assertEqual(
            MODULE.select_issue([issue(20, "P2", title="[Native] C ABI Binding")])[
                "number"
            ],
            20,
        )

    def test_claimed_issue_is_skipped(self):
        selected = MODULE.select_issue(
            [issue(514, "P0"), issue(20, "P1")], claimed={514}
        )
        self.assertEqual(selected["number"], 20)

    def test_all_claimed_returns_none(self):
        self.assertIsNone(MODULE.select_issue([issue(514, "P0")], claimed={514}))

    def test_claimed_numbers_come_from_issue_branches(self):
        pages = [[{"name": "main"}, {"name": "claude/issue-514-trust-pin"}, {"name": "codex/issue-7"}]]
        original = MODULE.gh_json
        MODULE.gh_json = lambda args: pages.pop(0)
        try:
            self.assertEqual(MODULE.fetch_claimed_issue_numbers(), {514, 7})
        finally:
            MODULE.gh_json = original

    def test_commented_claim_is_skipped_in_priority_order(self):
        original = MODULE.has_claim_comment
        MODULE.has_claim_comment = lambda number: number == 92
        try:
            selected = MODULE.select_unclaimed_issue(
                [issue(92, "P1"), issue(30, "P2"), issue(40, "P3")], set()
            )
        finally:
            MODULE.has_claim_comment = original
        self.assertEqual(selected["number"], 30)


if __name__ == "__main__":
    unittest.main()
