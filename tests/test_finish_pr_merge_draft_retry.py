"""Issue #807: Draft PRの事前停止、merge失敗本文の表示、読取専用照会の有界再試行。"""

from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from subprocess import CompletedProcess
from unittest.mock import patch

from tools import finish_pr_merge as gate

URL = "https://github.com/tomiya7688/AI_game_player/pull/807"
HEAD = "a" * 40
HEAD_REF = "claude/issue-807-merge-gate-draft-retry"
TLS_ERROR = 'Get "https://api.github.com/graphql": net/http: TLS handshake timeout'


def completed(
    payload: object = "", code: int = 0, stderr: str = ""
) -> CompletedProcess[str]:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return CompletedProcess(["gh"], code, stdout, stderr)


def pr_view(
    is_draft: object = False, omit_draft: bool = False
) -> CompletedProcess[str]:
    payload: dict[str, object] = {
        "state": "OPEN",
        "isDraft": is_draft,
        "mergeable": "MERGEABLE",
        "headRefOid": HEAD,
        "headRefName": HEAD_REF,
    }
    if omit_draft:
        del payload["isDraft"]
    return completed(payload)


THREADS = {
    "data": {
        "repository": {
            "pullRequest": {
                "state": "OPEN",
                "reviewThreads": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
    }
}
CHECKS = [{"name": "tests", "state": "SUCCESS", "bucket": "pass"}]
MERGED = {
    "number": 807,
    "state": "closed",
    "merged": True,
    "head": {"sha": HEAD},
    "merge_commit_sha": "b" * 40,
}
STILL_OPEN = {
    "number": 807,
    "state": "open",
    "merged": False,
    "head": {"sha": HEAD},
    "merge_commit_sha": None,
}


class FakeGitHub:
    """gh呼出しを種類別の応答列で返す。列が尽きたら最後の応答を繰り返す。"""

    def __init__(self, **responses: list[object]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[list[str]] = []
        self.timeouts: list[int] = []

    @staticmethod
    def kind(arguments: list[str]) -> str:
        if arguments[:2] == ["pr", "view"]:
            return "view"
        if arguments[:2] == ["pr", "checks"]:
            return "watch" if "--watch" in arguments else "checks"
        if arguments[:2] == ["api", "graphql"]:
            return "threads"
        if arguments[:2] == ["pr", "merge"]:
            return "merge"
        return "confirm"

    def __call__(self, arguments: list[str], timeout: int) -> CompletedProcess[str]:
        self.calls.append(arguments)
        self.timeouts.append(timeout)
        kind = self.kind(arguments)
        queue = self.responses.get(kind)
        if not queue:
            defaults = {
                "view": pr_view(),
                "watch": completed("ok"),
                "checks": completed(CHECKS),
                "threads": completed(THREADS),
                "merge": completed("Merged"),
                "confirm": completed(MERGED),
            }
            response: object = defaults[kind]
        else:
            response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, CompletedProcess)
        return response

    def count(self, kind: str) -> int:
        return sum(self.kind(call) == kind for call in self.calls)


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        patches = [
            patch.object(gate, "_sleep", side_effect=self.sleeps.append),
            patch.object(gate, "_require_workflow_runs"),
        ]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)

    def run_gate(self, fake: FakeGitHub) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(gate, "_run_gh", side_effect=fake),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = gate.main([URL])
        return code, stdout.getvalue(), stderr.getvalue()


class DraftDetectionTests(GateTestCase):
    def test_draft_pr_stops_before_waiting_for_checks(self) -> None:
        fake = FakeGitHub(view=[pr_view(is_draft=True)])
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertIn("Pull request is a draft", stderr)
        self.assertIn("does not change draft status", stderr)
        self.assertEqual(["view"], [fake.kind(call) for call in fake.calls])
        self.assertIn("isDraft", fake.calls[0][-1])

    def test_missing_or_non_boolean_draft_status_fails_closed(self) -> None:
        for response in (
            pr_view(omit_draft=True),
            pr_view(is_draft="false"),
            pr_view(is_draft=None),
        ):
            with self.subTest(response=response.stdout):
                fake = FakeGitHub(view=[response])
                code, _, stderr = self.run_gate(fake)
                self.assertEqual(3, code)
                self.assertIn("draft status", stderr)
                self.assertEqual(0, fake.count("watch"))

    def test_pr_converted_to_draft_during_checks_is_not_merged(self) -> None:
        fake = FakeGitHub(view=[pr_view(), pr_view(is_draft=True)])
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertIn("Pull request is a draft", stderr)
        self.assertEqual(0, fake.count("merge"))

    def test_ready_pr_still_merges(self) -> None:
        fake = FakeGitHub()
        code, stdout, _ = self.run_gate(fake)
        self.assertEqual(0, code)
        self.assertIn("[MERGED]", stdout)
        self.assertEqual(1, fake.count("merge"))


class MergeFailureTextTests(GateTestCase):
    def test_github_rejection_text_is_reported_when_pr_is_still_open(self) -> None:
        fake = FakeGitHub(
            merge=[
                completed(
                    code=1,
                    stderr="GraphQL: Pull Request is still a draft (mergePullRequest)",
                )
            ],
            confirm=[completed(STILL_OPEN)],
        )
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertIn("PR was not merged", stderr)
        self.assertIn("still open and unmerged", stderr)
        self.assertIn("Pull Request is still a draft", stderr)
        self.assertNotIn("unconfirmed", stderr)
        self.assertEqual(1, fake.count("merge"))

    def test_rejection_text_leads_unconfirmed_message_when_confirmation_fails(
        self,
    ) -> None:
        fake = FakeGitHub(
            merge=[
                completed(
                    code=1,
                    stderr="GraphQL: Pull Request is still a draft (mergePullRequest)",
                )
            ],
            confirm=[completed(code=1, stderr="HTTP 401: Bad credentials")],
        )
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertIn(
            "[STOP] Merge command failed: GraphQL: Pull Request is still a draft",
            stderr,
        )
        self.assertIn("unconfirmed", stderr)
        self.assertEqual(1, fake.count("merge"))

    def test_timeout_with_open_pr_is_not_reported_as_rejection(self) -> None:
        fake = FakeGitHub(
            merge=[
                gate.GitHubCommandTimeout("GitHub CLI did not finish within 60 seconds")
            ],
            confirm=[completed(STILL_OPEN)],
        )
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertIn("unconfirmed", stderr)
        self.assertNotIn("PR was not merged", stderr)
        self.assertEqual(1, fake.count("merge"))

    def test_transient_merge_error_with_open_pr_is_not_reported_as_rejection(
        self,
    ) -> None:
        fake = FakeGitHub(
            merge=[completed(code=1, stderr=TLS_ERROR)], confirm=[completed(STILL_OPEN)]
        )
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertIn("Merge command failed: ", stderr)
        self.assertIn("unconfirmed", stderr)
        self.assertNotIn("PR was not merged", stderr)
        self.assertEqual(1, fake.count("merge"))

    def test_zero_exit_with_open_pr_is_unconfirmed(self) -> None:
        fake = FakeGitHub(confirm=[completed(STILL_OPEN)])
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertIn("unconfirmed", stderr)
        self.assertNotIn("PR was not merged", stderr)


class ReadOnlyRetryTests(GateTestCase):
    def test_transient_lookup_failures_are_retried_with_spacing(self) -> None:
        fake = FakeGitHub(
            checks=[
                completed(code=1, stderr=TLS_ERROR),
                completed(code=1, stderr=TLS_ERROR),
                completed(CHECKS),
            ]
        )
        code, stdout, _ = self.run_gate(fake)
        self.assertEqual(0, code)
        self.assertEqual([10, 30], self.sleeps)
        self.assertEqual(3, fake.count("checks"))
        self.assertIn("[RETRY] Pull request status check lookup", stdout)

    def test_retries_are_bounded(self) -> None:
        fake = FakeGitHub(threads=[completed(code=1, stderr=TLS_ERROR)])
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertEqual(3, fake.count("threads"))
        self.assertEqual([10, 30], self.sleeps)
        self.assertIn("after 3 attempts", stderr)
        self.assertEqual(0, fake.count("merge"))

    def test_non_transient_failures_are_not_retried(self) -> None:
        for stderr_text in (
            "HTTP 401: Bad credentials",
            "HTTP 429: Too Many Requests",
            "HTTP 404: Not Found",
        ):
            with self.subTest(stderr=stderr_text):
                self.sleeps.clear()
                fake = FakeGitHub(view=[completed(code=1, stderr=stderr_text)])
                code, _, _ = self.run_gate(fake)
                self.assertEqual(3, code)
                self.assertEqual(1, fake.count("view"))
                self.assertEqual([], self.sleeps)

    def test_api_timeout_is_retried(self) -> None:
        fake = FakeGitHub(view=[gate.GitHubCommandTimeout("timed out"), pr_view()])
        code, _, _ = self.run_gate(fake)
        self.assertEqual(0, code)
        self.assertEqual([10], self.sleeps)

    def test_watch_timeout_is_not_retried(self) -> None:
        fake = FakeGitHub(
            watch=[
                gate.GitHubCommandTimeout(
                    "GitHub CLI did not finish within 3600 seconds"
                )
            ]
        )
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertEqual(1, fake.count("watch"))
        self.assertEqual([], self.sleeps)
        self.assertIn("Status checks did not finish successfully", stderr)

    def test_watch_failure_from_checks_is_not_retried(self) -> None:
        fake = FakeGitHub(
            watch=[completed("tests\tfail\t1m\thttps://example.invalid", code=1)]
        )
        code, _, _ = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertEqual(1, fake.count("watch"))

    def test_watch_retry_shares_the_original_time_budget(self) -> None:
        clock = iter([1000.0, 1000.0, 2000.0, 2000.0])
        fake = FakeGitHub(watch=[completed(code=1, stderr=TLS_ERROR), completed("ok")])
        with patch.object(gate, "_monotonic", side_effect=lambda: next(clock)):
            code, _, _ = self.run_gate(fake)
        self.assertEqual(0, code)
        watch_timeouts = [
            t
            for call, t in zip(fake.calls, fake.timeouts)
            if fake.kind(call) == "watch"
        ]
        self.assertEqual(
            [
                gate.CHECK_COMMAND_TIMEOUT_SECONDS,
                gate.CHECK_COMMAND_TIMEOUT_SECONDS - 1000,
            ],
            watch_timeouts,
        )

    def test_watch_retry_stops_when_budget_is_exhausted(self) -> None:
        budget = gate.CHECK_COMMAND_TIMEOUT_SECONDS
        clock = iter([0.0, 0.0, budget - 5.0])
        fake = FakeGitHub(watch=[completed(code=1, stderr=TLS_ERROR), completed("ok")])
        with patch.object(gate, "_monotonic", side_effect=lambda: next(clock)):
            code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertEqual(1, fake.count("watch"))
        self.assertIn("no time budget left", stderr)


class WorkflowLookupRetryTests(unittest.TestCase):
    def test_workflow_run_lookup_is_retried(self) -> None:
        sleeps: list[float] = []
        reads: list[object] = []

        class FakeWorkflowGate:
            def __init__(self, repository, number, head_sha, head_ref, read_json):
                self.read_json = read_json

            def verify(self) -> None:
                reads.append(
                    self.read_json(
                        "repos/tomiya7688/AI_game_player/actions/runs?event=push"
                    )
                )

        fake = FakeGitHub(
            confirm=[
                completed(code=1, stderr=TLS_ERROR),
                completed({"workflow_runs": []}),
            ]
        )
        with (
            patch.object(gate, "WorkflowRunGate", FakeWorkflowGate),
            patch.object(gate, "_run_gh", side_effect=fake),
            patch.object(gate, "_sleep", side_effect=sleeps.append),
            redirect_stdout(StringIO()),
        ):
            gate._require_workflow_runs(
                gate.parse_pull_request_reference(URL), HEAD, HEAD_REF
            )
        self.assertEqual([{"workflow_runs": []}], reads)
        self.assertEqual([10], sleeps)
        self.assertEqual(2, fake.count("confirm"))


class MergeIsNeverRepeatedTests(GateTestCase):
    def test_transient_merge_failure_is_not_repeated_but_confirmation_is_retried(
        self,
    ) -> None:
        fake = FakeGitHub(
            merge=[completed(code=1, stderr=TLS_ERROR)],
            confirm=[completed(code=1, stderr=TLS_ERROR), completed(MERGED)],
        )
        code, stdout, _ = self.run_gate(fake)
        self.assertEqual(0, code)
        self.assertEqual(1, fake.count("merge"))
        self.assertEqual(2, fake.count("confirm"))
        self.assertIn("[MERGED]", stdout)

    def test_merge_timeout_is_not_repeated(self) -> None:
        fake = FakeGitHub(
            merge=[gate.GitHubCommandTimeout("timed out")],
            confirm=[completed(code=1, stderr=TLS_ERROR)],
        )
        code, _, stderr = self.run_gate(fake)
        self.assertEqual(3, code)
        self.assertEqual(1, fake.count("merge"))
        self.assertEqual(3, fake.count("confirm"))
        self.assertIn("do not repeat the merge blindly", stderr)


if __name__ == "__main__":
    unittest.main()
