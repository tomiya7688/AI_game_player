from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from tools import finish_pr_merge

PULL_REQUEST_URL = "https://github.com/tomiya7688/AI_game_player/pull/466"
PULL_REQUEST_HEAD_SHA = "a" * 40


def _completed_process(
    stdout: str = "",
    stderr: str = "",
    return_code: int = 0,
) -> CompletedProcess[str]:
    return CompletedProcess(args=["gh"], returncode=return_code, stdout=stdout, stderr=stderr)


def _pull_request_snapshot(head_sha: str = PULL_REQUEST_HEAD_SHA, state: str = "OPEN") -> CompletedProcess[str]:
    return _completed_process(
        json.dumps({"state": state, "mergeable": "MERGEABLE", "headRefOid": head_sha})
    )


def _review_thread_response(
    threads: list[dict[str, object]] | None = None,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> CompletedProcess[str]:
    return _completed_process(
        json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "state": "OPEN",
                            "reviewThreads": {
                                "nodes": threads or [],
                                "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                            },
                        }
                    }
                }
            }
        )
    )


def _passing_checks_response(checks: list[dict[str, object]] | None = None) -> CompletedProcess[str]:
    response_checks = checks if checks is not None else [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}]
    return _completed_process(json.dumps(response_checks))


class FinishPullRequestMergeTests(unittest.TestCase):
    def setUp(self):
        # event別REST検証はtest_finish_pr_workflow_runs.pyでmerge入口を含めて検査する。
        workflow_patch = patch("tools.finish_pr_merge._require_workflow_runs")
        workflow_patch.start()
        self.addCleanup(workflow_patch.stop)

    @patch("tools.finish_pr_merge._run_gh")
    def test_all_checks_pass_and_empty_review_threads_allow_merge(self, run_gh_mock: unittest.mock.Mock) -> None:
        run_gh_mock.side_effect = [
            _pull_request_snapshot(),
            _completed_process("All status checks passed."),
            _passing_checks_response(),
            _review_thread_response(),
            _pull_request_snapshot(),
            _completed_process("Merged"),
            _completed_process(json.dumps({"number": 466, "state": "closed", "merged": True,
                                           "head": {"sha": PULL_REQUEST_HEAD_SHA}, "merge_commit_sha": "b" * 40})),
        ]
        standard_output = StringIO()
        standard_error = StringIO()

        with redirect_stdout(standard_output), redirect_stderr(standard_error):
            exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(0, exit_code)
        self.assertIn("No unresolved inline review comments", standard_output.getvalue())
        self.assertIn("All 1 reported status checks passed", standard_output.getvalue())
        self.assertEqual("", standard_error.getvalue())
        merge_arguments = run_gh_mock.call_args_list[-2].args[0]
        self.assertEqual(
            [
                "pr",
                "merge",
                PULL_REQUEST_URL,
                "--merge",
                "--match-head-commit",
                PULL_REQUEST_HEAD_SHA,
            ],
            merge_arguments,
        )

    def test_failed_check_wait_command_stops_before_review_lookup_or_merge(self) -> None:
        with patch(
            "tools.finish_pr_merge._run_gh",
            side_effect=[_pull_request_snapshot(), _completed_process(stderr="CI check failed", return_code=1)],
        ) as run_gh_mock:
            standard_error = StringIO()
            with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(3, exit_code)
        self.assertIn("CI check failed", standard_error.getvalue())
        self.assertIn(PULL_REQUEST_URL, standard_error.getvalue())
        self.assertIn(f"gh pr checks {PULL_REQUEST_URL}", standard_error.getvalue())
        self.assertEqual(2, run_gh_mock.call_count)
        self.assertNotIn("merge", run_gh_mock.call_args_list[-1].args[0])

    def test_pending_failed_cancelled_skipped_and_unknown_checks_block_merge(self) -> None:
        non_passing_buckets = ["pending", "fail", "cancel", "skipping", "unexpected"]
        for check_bucket in non_passing_buckets:
            with self.subTest(check_bucket=check_bucket):
                with patch(
                    "tools.finish_pr_merge._run_gh",
                    side_effect=[
                        _pull_request_snapshot(),
                        _completed_process("CI watcher completed"),
                        _passing_checks_response(
                            [{"name": "release-check", "state": check_bucket.upper(), "bucket": check_bucket}]
                        ),
                    ],
                ) as run_gh_mock:
                    standard_error = StringIO()
                    with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                        exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

                self.assertEqual(3, exit_code)
                self.assertIn("Every status check must pass", standard_error.getvalue())
                self.assertEqual(3, run_gh_mock.call_count)

    def test_skipped_advisory_review_app_check_does_not_block_merge(self) -> None:
        checks = [
            {"name": "tests", "state": "SUCCESS", "bucket": "pass", "workflow": "tests"},
            {"name": "CodeRabbit", "state": "NEUTRAL", "bucket": "skipping", "workflow": ""},
            {"name": "Sourcery review", "state": "NEUTRAL", "bucket": "skipping", "workflow": ""},
        ]
        merged = json.dumps({"number": 466, "state": "closed", "merged": True,
                             "head": {"sha": PULL_REQUEST_HEAD_SHA}, "merge_commit_sha": "b" * 40})
        with patch(
            "tools.finish_pr_merge._run_gh",
            side_effect=[
                _pull_request_snapshot(),
                _completed_process("CI watcher completed"),
                _passing_checks_response(checks),
                _review_thread_response(),
                _pull_request_snapshot(),
                _completed_process("Merged"),
                _completed_process(merged),
            ],
        ):
            standard_output = StringIO()
            with redirect_stdout(standard_output), redirect_stderr(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(0, exit_code)
        self.assertIn("Optional AI review checks did not run", standard_output.getvalue())
        self.assertIn("CodeRabbit: skipping", standard_output.getvalue())
        self.assertIn("Sourcery review: skipping", standard_output.getvalue())
        self.assertIn("All 1 other reported status checks passed", standard_output.getvalue())

    def test_advisory_exception_does_not_cover_failures_or_actions_jobs(self) -> None:
        cases = {
            "advisory app failed": {"name": "CodeRabbit", "state": "FAILURE", "bucket": "fail", "workflow": ""},
            "advisory app pending": {"name": "Sourcery review", "state": "PENDING", "bucket": "pending", "workflow": ""},
            "actions job reusing the name": {
                "name": "CodeRabbit", "state": "SKIPPED", "bucket": "skipping", "workflow": "tests"
            },
            "workflow field missing": {"name": "CodeRabbit", "state": "NEUTRAL", "bucket": "skipping"},
            "advisory app cancelled": {"name": "CodeRabbit", "state": "CANCELLED", "bucket": "cancel", "workflow": ""},
            "name with different case": {"name": "coderabbit", "state": "NEUTRAL", "bucket": "skipping", "workflow": ""},
            "name with trailing space": {"name": "CodeRabbit ", "state": "NEUTRAL", "bucket": "skipping", "workflow": ""},
        }
        for label, check in cases.items():
            with self.subTest(label):
                with patch(
                    "tools.finish_pr_merge._run_gh",
                    side_effect=[
                        _pull_request_snapshot(),
                        _completed_process("CI watcher completed"),
                        _passing_checks_response([check]),
                    ],
                ) as run_gh_mock:
                    standard_error = StringIO()
                    with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                        exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

                self.assertEqual(3, exit_code)
                self.assertIn("Every status check must pass", standard_error.getvalue())
                self.assertEqual(3, run_gh_mock.call_count)

    def test_advisory_skips_do_not_hide_a_real_failure_or_stand_in_for_ci(self) -> None:
        sourcery = {"name": "Sourcery review", "state": "NEUTRAL", "bucket": "skipping", "workflow": ""}
        coderabbit = {"name": "CodeRabbit", "state": "NEUTRAL", "bucket": "skipping", "workflow": ""}
        failing = {"name": "Python 3.10", "state": "FAILURE", "bucket": "fail", "workflow": "tests"}
        cases = {
            "advisory skip beside a failure": ([coderabbit, sourcery, failing], "Every status check must pass"),
            "only advisory checks": ([coderabbit, sourcery], "CI is unverified"),
        }
        for label, (checks, message) in cases.items():
            with self.subTest(label):
                with patch(
                    "tools.finish_pr_merge._run_gh",
                    side_effect=[
                        _pull_request_snapshot(),
                        _completed_process("CI watcher completed"),
                        _passing_checks_response(checks),
                    ],
                ):
                    standard_error = StringIO()
                    with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                        exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

                self.assertEqual(3, exit_code)
                self.assertIn(message, standard_error.getvalue())
                if label == "advisory skip beside a failure":
                    self.assertIn("Python 3.10: fail", standard_error.getvalue())
                    self.assertNotIn("Sourcery review", standard_error.getvalue())

    def test_no_status_checks_block_merge_even_when_watch_exits_successfully(self) -> None:
        with patch(
            "tools.finish_pr_merge._run_gh",
            side_effect=[
                _pull_request_snapshot(),
                _completed_process("No status checks"),
                _passing_checks_response([]),
                _review_thread_response(),
            ],
        ) as run_gh_mock:
            standard_error = StringIO()
            with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(3, exit_code)
        self.assertIn("no status checks", standard_error.getvalue())
        self.assertEqual(3, run_gh_mock.call_count)

    def test_unresolved_review_comment_location_and_body_are_displayed_and_block_merge(self) -> None:
        unresolved_thread = {
            "isResolved": False,
            "path": "finish_pr.bat",
            "line": 72,
            "originalLine": 72,
            "comments": {
                "nodes": [
                    {
                        "body": "Do not merge until checks pass.",
                        "url": "https://github.com/tomiya7688/AI_game_player/pull/466#discussion_r1",
                        "author": {"login": "reviewer"},
                    }
                ]
            },
        }
        with patch(
            "tools.finish_pr_merge._run_gh",
            side_effect=[
                _pull_request_snapshot(),
                _completed_process("CI watcher completed"),
                _passing_checks_response(),
                _review_thread_response([unresolved_thread]),
            ],
        ) as run_gh_mock:
            standard_error = StringIO()
            with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(3, exit_code)
        self.assertIn("finish_pr.bat:72", standard_error.getvalue())
        self.assertIn("Do not merge until checks pass.", standard_error.getvalue())
        self.assertEqual(4, run_gh_mock.call_count)

    def test_github_authentication_error_stops_before_status_checks(self) -> None:
        with patch(
            "tools.finish_pr_merge._run_gh",
            return_value=_completed_process(stderr="authentication required", return_code=1),
        ) as run_gh_mock:
            standard_error = StringIO()
            with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(3, exit_code)
        self.assertIn("authentication required", standard_error.getvalue())
        self.assertEqual(1, run_gh_mock.call_count)

    def test_unknown_pull_request_state_stops_before_status_checks(self) -> None:
        with patch(
            "tools.finish_pr_merge._run_gh",
            return_value=_pull_request_snapshot(state="UNKNOWN"),
        ) as run_gh_mock:
            standard_error = StringIO()
            with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(3, exit_code)
        self.assertIn("not OPEN", standard_error.getvalue())
        self.assertEqual(1, run_gh_mock.call_count)

    def test_changed_head_commit_stops_before_merge(self) -> None:
        updated_head_sha = "b" * 40
        with patch(
            "tools.finish_pr_merge._run_gh",
            side_effect=[
                _pull_request_snapshot(),
                _completed_process("CI watcher completed"),
                _passing_checks_response(),
                _review_thread_response(),
                _pull_request_snapshot(head_sha=updated_head_sha),
            ],
        ) as run_gh_mock:
            standard_error = StringIO()
            with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(3, exit_code)
        self.assertIn("head changed", standard_error.getvalue())
        self.assertEqual(5, run_gh_mock.call_count)

    def test_review_thread_pagination_checks_comments_on_later_pages(self) -> None:
        unresolved_thread = {
            "isResolved": False,
            "path": "tools/finish_pr_merge.py",
            "line": 10,
            "originalLine": 10,
            "comments": {
                "nodes": [
                    {
                        "body": "Second page comment.",
                        "url": "https://github.com/tomiya7688/AI_game_player/pull/466#discussion_r2",
                        "author": {"login": "reviewer"},
                    }
                ]
            },
        }
        with patch(
            "tools.finish_pr_merge._run_gh",
            side_effect=[
                _pull_request_snapshot(),
                _completed_process("CI watcher completed"),
                _passing_checks_response(),
                _review_thread_response(has_next_page=True, end_cursor="cursor-1"),
                _review_thread_response([unresolved_thread]),
            ],
        ) as run_gh_mock:
            standard_error = StringIO()
            with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                exit_code = finish_pr_merge.main([PULL_REQUEST_URL])

        self.assertEqual(3, exit_code)
        self.assertIn("Second page comment.", standard_error.getvalue())
        self.assertIn("cursor=cursor-1", run_gh_mock.call_args_list[4].args[0])

    def test_non_github_and_malformed_pull_request_urls_are_rejected(self) -> None:
        for pull_request_url in [
            "http://github.com/tomiya7688/AI_game_player/pull/466",
            "https://example.com/tomiya7688/AI_game_player/pull/466",
            "https://github.com:invalid/tomiya7688/AI_game_player/pull/466",
            "https://github.com/tomiya7688/AI_game_player/issues/466",
            "https://github.com/tomiya7688/AI_game_player/pull/0",
        ]:
            with self.subTest(pull_request_url=pull_request_url):
                with patch("tools.finish_pr_merge._run_gh") as run_gh_mock:
                    standard_error = StringIO()
                    with redirect_stderr(standard_error), redirect_stdout(StringIO()):
                        exit_code = finish_pr_merge.main([pull_request_url])

                self.assertEqual(3, exit_code)
                run_gh_mock.assert_not_called()

    def test_batch_script_calls_merge_gate_before_switching_to_main(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        batch_script = (repository_root / "finish_pr.bat").read_text(encoding="utf-8")
        gate_invocation = 'python tools\\finish_pr_merge.py "%PR_URL%"'

        self.assertIn(gate_invocation, batch_script)
        self.assertNotIn("gh pr merge", batch_script.lower())
        self.assertIn("goto VALIDATE_PR", batch_script)
        self.assertIn("Reusing PR:", batch_script)
        self.assertIn("if exist AGENTS.override.md git reset -- AGENTS.override.md", batch_script)
        self.assertLess(batch_script.index(gate_invocation), batch_script.index("git switch main"))


if __name__ == "__main__":
    unittest.main()
