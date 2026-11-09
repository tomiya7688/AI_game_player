import subprocess
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


class PlanRerunTest(unittest.TestCase):
    def test_waits_while_any_run_is_in_progress(self):
        targets, reason = pr_digest.plan_rerun(
            [run(1, "in_progress", None), run(2, conclusion="failure")]
        )
        self.assertEqual((targets, "in progress" in reason), ([], True))

    def test_nothing_failed(self):
        self.assertEqual(
            pr_digest.plan_rerun([run(1), run(2)]), ([], "nothing to re-run")
        )

    def test_already_rerun_once_is_not_rerun_again(self):
        targets, reason = pr_digest.plan_rerun(
            [run(1, conclusion="failure", attempt=2, name="win")]
        )
        self.assertEqual(targets, [])
        self.assertIn("already re-run once: win", reason)

    def test_failure_and_timed_out_are_rerun_but_action_required_is_not(self):
        runs = [
            run(1, conclusion="failure"),
            run(2, conclusion="timed_out"),
            run(3, conclusion="action_required"),
            run(4),
        ]
        targets, _ = pr_digest.plan_rerun(runs)
        self.assertEqual([t["id"] for t in targets], [1, 2])


class RerunFailedTest(unittest.TestCase):
    def rerun(self, runs, post_effect=None, head="sha", reruns=None):
        """runsを返すworkflow_runsと、PRのhead SHAを返すgh_jsonを差し替えて実行する。"""
        sequence = [runs] if reruns is None else [runs, reruns]
        with (
            patch.object(pr_digest, "workflow_runs", side_effect=sequence * 2),
            patch.object(pr_digest, "gh_json", return_value={"head": {"sha": head}}),
            patch.object(pr_digest, "post_rerun", side_effect=post_effect) as post,
        ):
            result = pr_digest.rerun_failed("o/r", 1, "sha", "branch")
        return result, post

    def test_reruns_each_failed_run_once(self):
        runs = [
            run(1),
            run(2, conclusion="failure", name="win"),
            run(3, conclusion="failure", name="cpp"),
        ]
        (lines, ok), post = self.rerun(runs)
        self.assertTrue(ok)
        self.assertEqual(
            [c.args for c in post.call_args_list], [("o/r", 2), ("o/r", 3)]
        )
        self.assertTrue(all("requested" in line for line in lines))

    def test_does_not_post_when_the_plan_is_empty(self):
        (lines, ok), post = self.rerun([run(1, "in_progress", None)])
        self.assertTrue(ok)
        post.assert_not_called()
        self.assertIn("in progress", lines[0])

    def test_does_not_post_when_the_pr_head_moved_since_the_read(self):
        (lines, ok), post = self.rerun([run(2, conclusion="failure")], head="newsha123")
        self.assertTrue(ok)
        post.assert_not_called()
        self.assertIn("PR head moved to newsha1", lines[0])

    def test_does_not_post_when_a_run_started_between_the_two_reads(self):
        failed = [run(2, conclusion="failure")]
        recheck = [run(2, conclusion="failure"), run(9, "in_progress", None)]
        (lines, ok), post = self.rerun(failed, reruns=recheck)
        post.assert_not_called()
        self.assertIn("plan changed on re-check", lines[0])

    def test_a_failed_post_keeps_the_results_already_requested(self):
        runs = [
            run(2, conclusion="failure", name="win"),
            run(3, conclusion="failure", name="cpp"),
        ]

        def effect(_repo, run_id):
            if run_id == 3:
                raise subprocess.CalledProcessError(1, "gh")

        (lines, ok), _ = self.rerun(runs, effect)
        self.assertFalse(ok)
        self.assertIn("requested win", lines[0])
        self.assertIn("ERROR cpp", lines[1])


class WorkflowRunsTest(unittest.TestCase):
    def test_keeps_only_runs_of_the_pr_branch_and_deduplicates_by_id(self):
        runs = [
            {"id": 1, "head_branch": "pr-branch"},
            {"id": 1, "head_branch": "pr-branch"},
            {"id": 2, "head_branch": "other-branch"},
        ]
        with patch.object(pr_digest, "fetch_pages", return_value=runs):
            result = pr_digest.workflow_runs("o/r", "sha", "pr-branch")
        self.assertEqual([r["id"] for r in result], [1])


class SafetyTest(unittest.TestCase):
    def test_terminal_control_characters_are_removed(self):
        self.assertEqual(pr_digest.safe_text("a\x1b[31mred\x07b"), "a [31mred b")

    def test_a_deleted_account_is_reported_as_ghost_instead_of_crashing(self):
        self.assertEqual(pr_digest.login_of({"user": None}), "ghost")
        comments = [{"id": 1, "user": None, "body": "x", "path": "a", "line": 1}]
        self.assertEqual(pr_digest.inline_findings(comments), [])


class DigestTest(unittest.TestCase):
    def digest(self, reviews):
        pull = {
            "title": "t\x1b[2Jitle",
            "state": "open",
            "draft": False,
            "mergeable_state": "clean",
            "head": {"sha": "abcdef123", "ref": "branch"},
        }
        pages = {
            "check_runs": [
                {"name": "ci\x1b", "conclusion": "success", "status": "completed"}
            ],
        }

        def fetch(_repo, path, key=None):
            if key == "check_runs":
                self.assertIn("filter=latest", path)
                return pages["check_runs"]
            return reviews if "reviews" in path else []

        with (
            patch.object(pr_digest, "gh_json", return_value=pull),
            patch.object(pr_digest, "fetch_pages", side_effect=fetch),
        ):
            return pr_digest.digest("o/r", 7)

    def test_only_each_humans_latest_review_state_is_shown_and_text_is_sanitized(self):
        reviews = [
            {"user": {"login": "owner"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "owner"}, "state": "APPROVED"},
            {"user": {"login": "x[bot]"}, "state": "COMMENTED"},
            {"user": None, "state": "COMMENTED"},
        ]
        lines, _, sha, branch = self.digest(reviews)
        text = "\n".join(lines)
        self.assertIn("owner:APPROVED", text)
        self.assertNotIn("CHANGES_REQUESTED", text)
        self.assertIn("ghost:COMMENTED", text)
        self.assertNotIn("\x1b", text)
        self.assertEqual((sha, branch), ("abcdef123", "branch"))


class FetchPagesTest(unittest.TestCase):
    def test_collects_pages_until_a_short_page(self):
        pages = [[{"n": i} for i in range(100)], [{"n": 1}]]
        with patch.object(pr_digest, "gh_json", side_effect=pages):
            self.assertEqual(len(pr_digest.fetch_pages("o/r", "x")), 101)

    def test_raises_when_pagination_never_ends(self):
        with patch.object(pr_digest, "gh_json", return_value=[{"n": 0}] * 100):
            with self.assertRaises(ValueError):
                pr_digest.fetch_pages("o/r", "x")


class MainExitCodeTest(unittest.TestCase):
    def test_returns_nonzero_when_a_pr_cannot_be_read(self):
        error = subprocess.CalledProcessError(1, "gh")
        with (
            patch.object(pr_digest, "digest", side_effect=error),
            patch("builtins.print"),
        ):
            self.assertEqual(pr_digest.main(["1"]), 1)

    def test_returns_nonzero_when_a_rerun_fails_and_zero_otherwise(self):
        with (
            patch.object(
                pr_digest, "digest", return_value=(["#1"], [], "sha", "branch")
            ),
            patch.object(
                pr_digest, "rerun_failed", return_value=(["  rerun: ERROR"], False)
            ),
            patch("builtins.print"),
        ):
            self.assertEqual(pr_digest.main(["1", "--rerun-failed"]), 1)
        with (
            patch.object(
                pr_digest, "digest", return_value=(["#1"], [], "sha", "branch")
            ),
            patch("builtins.print"),
        ):
            self.assertEqual(pr_digest.main(["1"]), 0)


if __name__ == "__main__":
    unittest.main()
