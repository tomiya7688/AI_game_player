import unittest
from unittest.mock import patch

from tools import pr_digest


def run(run_id, status="completed", conclusion="success", attempt=1, name="ci"):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
    }


class CountChecksTest(unittest.TestCase):
    def test_counts_conclusions_and_lists_failed_names(self):
        runs = [
            {"name": "a", "conclusion": "success", "status": "completed"},
            {"name": "b", "conclusion": "failure", "status": "completed"},
            {"name": "c", "conclusion": None, "status": "in_progress"},
        ]
        counts, failed = pr_digest.count_checks(runs)
        self.assertEqual(counts, {"success": 1, "failure": 1, "in_progress": 1})
        self.assertEqual(failed, ["b"])


class HeadlineTest(unittest.TestCase):
    def test_prefers_the_bold_conclusion_over_analysis_logs(self):
        body = "Script executed:\n```bash\nrg x\n```\nLength of output: 12\n\n**結論の見出し**\n\n本文\n"
        self.assertEqual(pr_digest.headline(body), "結論の見出し")

    def test_falls_back_to_first_text_line(self):
        self.assertEqual(pr_digest.headline("最初の行\n次の行"), "最初の行")
        self.assertEqual(pr_digest.headline(""), "(no headline)")


class InlineFindingsTest(unittest.TestCase):
    def test_marks_bot_findings_answered_only_by_a_human_reply(self):
        bot = {"login": "sourcery-ai[bot]"}
        human = {"login": "someone"}
        comments = [
            {"id": 1, "user": bot, "body": "**a**", "path": "x.py", "line": 3},
            {"id": 2, "user": bot, "body": "**b**", "path": "y.py", "line": 4},
            {"id": 3, "user": human, "body": "done", "in_reply_to_id": 1},
            {"id": 4, "user": bot, "body": "bot reply", "in_reply_to_id": 2},
        ]
        findings = pr_digest.inline_findings(comments)
        self.assertEqual(
            [(f["id"], f["answered"]) for f in findings], [(1, True), (2, False)]
        )


class RerunFailedTest(unittest.TestCase):
    def rerun(self, runs):
        with (
            patch.object(pr_digest, "workflow_runs", return_value=runs),
            patch.object(
                pr_digest.subprocess, "check_output", return_value="{}"
            ) as post,
        ):
            lines = pr_digest.rerun_failed("o/r", "sha")
        return lines, post

    def test_does_nothing_while_any_run_is_in_progress(self):
        lines, post = self.rerun(
            [run(1, "in_progress", None), run(2, conclusion="failure")]
        )
        self.assertIn("in progress", lines[0])
        post.assert_not_called()

    def test_does_nothing_when_nothing_failed(self):
        lines, post = self.rerun([run(1), run(2)])
        self.assertIn("nothing to re-run", lines[0])
        post.assert_not_called()

    def test_never_reruns_a_run_that_was_already_rerun_once(self):
        lines, post = self.rerun([run(1, conclusion="failure", attempt=2, name="win")])
        self.assertIn("already re-run once", lines[0])
        post.assert_not_called()

    def test_reruns_each_failed_run_once_when_all_runs_completed(self):
        lines, post = self.rerun(
            [
                run(1),
                run(2, conclusion="failure", name="win"),
                run(3, conclusion="failure", name="cpp"),
            ]
        )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            [call.args[0][-1] for call in post.call_args_list],
            [
                "repos/o/r/actions/runs/2/rerun-failed-jobs",
                "repos/o/r/actions/runs/3/rerun-failed-jobs",
            ],
        )
        self.assertTrue(all("requested" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
