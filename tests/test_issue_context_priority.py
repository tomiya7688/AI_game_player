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

    def test_fullwidth_colon_claim_comment_is_detected(self):
        original = MODULE.gh_json
        MODULE.gh_json = lambda args: [{"body": "着手：Codex, branch"}]
        try:
            self.assertTrue(MODULE.has_claim_comment(1))
        finally:
            MODULE.gh_json = original

    def test_claim_comment_on_later_page_is_found(self):
        pages = [[{"body": "雑談"}] * 100, [{"body": "着手: Codex"}]]
        original = MODULE.gh_json
        MODULE.gh_json = lambda args: pages.pop(0)
        try:
            self.assertTrue(MODULE.has_claim_comment(1))
        finally:
            MODULE.gh_json = original

    def test_rest_fallback_is_used_for_graphql_failures(self):
        import json
        import subprocess

        failures = (
            subprocess.CalledProcessError(1, "gh"),
            subprocess.TimeoutExpired("gh", 120),
            json.JSONDecodeError("bad", "", 0),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def graphql(*_args, **_kwargs):
                    raise failure

                original = (MODULE.fetch_issues_graphql, MODULE.fetch_issues_rest)
                MODULE.fetch_issues_graphql = graphql
                MODULE.fetch_issues_rest = lambda: ["rest"]
                try:
                    self.assertEqual(MODULE.fetch_issues(), ["rest"])
                finally:
                    MODULE.fetch_issues_graphql, MODULE.fetch_issues_rest = original

    def test_rest_listing_excludes_prs_and_handles_page_boundaries(self):
        def node(number, pull=False):
            item = {"number": number, "title": "t", "html_url": "u", "labels": [{"name": "P1"}]}
            return {**item, "pull_request": {}} if pull else item

        pages = [[node(n) for n in range(100)], [node(100), node(101, pull=True)]]
        original = MODULE.gh_json
        MODULE.gh_json = lambda args: pages.pop(0)
        try:
            numbers = [issue["number"] for issue in MODULE.fetch_issues_rest()]
        finally:
            MODULE.gh_json = original
        self.assertEqual(numbers, list(range(101)))

    def test_rest_listing_fails_closed_when_pagination_never_ends(self):
        original = MODULE.gh_json
        MODULE.gh_json = lambda args: [
            {"number": 1, "title": "t", "html_url": "u", "labels": []} for _ in range(100)
        ]
        try:
            with self.assertRaises(ValueError):
                MODULE.fetch_issues_rest()
        finally:
            MODULE.gh_json = original


if __name__ == "__main__":
    unittest.main()
