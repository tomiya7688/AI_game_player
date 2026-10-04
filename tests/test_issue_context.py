import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import issue_context as context


def issue(number, priority="P1", title="Work", body="## Acceptance\n- Preserve safety."):
    url = f"https://github.com/{context.REPO}/issues/{number}"
    return {"number": number, "title": title, "labels": [{"name": priority}],
            "body": body, "state": "open", "url": url, "html_url": url}


class IssueContextTests(unittest.TestCase):
    def test_p0_to_p5_then_unlabeled_with_oldest_tiebreak(self):
        for priority in range(6):
            candidates = [issue(20, f"P{priority}"), issue(10, f"p{priority}"), issue(1, "other")]
            candidates += [issue(2, f"P{lower}") for lower in range(priority + 1, 6)]
            self.assertEqual(context.select_issue(candidates)["number"], 10)

    def test_policy_and_parent_titles_or_labels_are_not_auto_selected(self):
        labeled = issue(3, "P0")
        labeled["labels"].append({"name": "parent"})
        candidates = [issue(19, "P0"), issue(1, "P0", "[Capture Parent] Group"),
                      issue(2, "P0", "UI / UX Parent：Group"), labeled,
                      issue(4, "P0", "親Issue：Group"), issue(5)]
        self.assertEqual(context.select_issue(candidates)["number"], 5)
        self.assertIsNone(context.select_issue(candidates[:-1]))

    def test_metadata_pagination_reads_no_bodies_or_comments(self):
        candidates = [issue(n, "P2") for n in range(1, 106)]
        candidates[-1]["labels"] = [{"name": "P0"}]
        pages = []
        for index, chunk in enumerate((candidates[:100], candidates[100:])):
            nodes = [{"number": i["number"], "title": i["title"], "url": i["url"],
                      "labels": {"nodes": i["labels"]}} for i in chunk]
            pages.append({"data": {"repository": {"issues": {"nodes": nodes,
                         "pageInfo": {"hasNextPage": index == 0, "endCursor": str(index)}}}}})
        with patch.object(context.subprocess, "check_output", return_value=json.dumps(pages)) as call:
            self.assertEqual(context.select_issue(context.fetch_issues())["number"], 105)
        self.assertIn("--paginate", call.call_args.args[0])
        self.assertNotIn("body", context.QUERY)
        self.assertNotIn("comments", context.QUERY)

    def test_incomplete_or_error_metadata_fails_closed(self):
        incomplete = [{"data": {"repository": {"issues": {
            "nodes": [], "pageInfo": {"hasNextPage": True}}}}}]
        for pages in (incomplete, [{"errors": [{"message": "denied"}]}]):
            with patch.object(context, "gh_json", return_value=pages), self.assertRaises(ValueError):
                context.fetch_issues()

    def test_explicit_issue_bypasses_list_even_for_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / ".codex/next_issue.md"
            with patch.object(context, "OUT", output), patch.object(context, "fetch_issues") as listing, \
                    patch.object(context, "fetch_issue", return_value=issue(27, title="[Codex Parent]")) as fetch:
                self.assertEqual(context.main(["--issue", "27"]), 0)
                listing.assert_not_called()
                fetch.assert_called_once_with(27)
            self.assertIn("#27", output.read_text(encoding="utf-8"))

    def test_only_selected_body_is_fetched(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(context, "OUT", Path(directory) / "pack.md"), \
                    patch.object(context, "fetch_issues", return_value=[issue(5), issue(1, "P0")]), \
                    patch.object(context, "fetch_issue", return_value=issue(1, "P0")) as fetch:
                self.assertEqual(context.main([]), 0)
                fetch.assert_called_once_with(1)

    def test_bounded_pack_preserves_headings_and_marks_truncation(self):
        selected = issue(1, body="## Goal\n" + "あ" * 10000)
        selected["labels"].append({"name": "spec:execution"})
        pack = context.render_context(selected, 2000)
        self.assertEqual(len(pack), 2000)
        self.assertIn("## Goal\n", pack)
        self.assertIn("TRUNCATED", pack)
        self.assertIn(selected["url"], pack)
        self.assertIn("doc/操作実行機能説明書.md", pack)
        self.assertIn("Discussion is not loaded", pack)
        self.assertIn("finish_task.bat", pack)

    def test_missing_body_does_not_invent_requirements(self):
        pack = context.render_context(issue(1, body=None))
        self.assertIn("clarify requirements", pack)
        self.assertNotIn("TRUNCATED", pack)

    def test_failed_or_empty_selection_never_overwrites_old_pack(self):
        for failure in (ValueError("No Issues"), subprocess.CalledProcessError(1, "gh"),
                        subprocess.TimeoutExpired("gh", 120)):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "pack.md"
                output.write_text("old", encoding="utf-8")
                with patch.object(context, "OUT", output), \
                        patch.object(context, "fetch_issues", side_effect=failure), \
                        patch("builtins.print") as printing:
                    self.assertEqual(context.main([]), 1)
                    self.assertIn("Do not use", printing.call_args.args[0])
                self.assertEqual(output.read_text(encoding="utf-8"), "old")
        with patch.object(context, "fetch_issues", return_value=[]), \
                patch.object(context, "fetch_issue") as fetch:
            self.assertEqual(context.main([]), 1)
            fetch.assert_not_called()

    def test_closed_issue_or_pr_is_rejected(self):
        for selected in ({**issue(1), "state": "closed"}, {**issue(1), "pull_request": {}}):
            with patch.object(context, "gh_json", return_value=selected), self.assertRaises(ValueError):
                context.fetch_issue(1)

    def test_invalid_cli_limits_fail_before_network(self):
        with patch.object(context, "fetch_issues") as listing:
            for args in (["--issue", "0"], ["--max-chars", "1999"], ["--max-chars", "16001"]):
                with self.assertRaises(SystemExit):
                    context.main(args)
            listing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
