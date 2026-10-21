import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from subprocess import CompletedProcess
from unittest.mock import patch

from tools import finish_pr_merge as gate


URL = "https://github.com/tomiya7688/AI_game_player/pull/473"
HEAD = "a" * 40
MERGE = "b" * 40


class MergeConfirmationTests(unittest.TestCase):
    def invoke(self, payload, merge_code=0, api_code=0):
        commands = []

        def gh(arguments, timeout):
            commands.append(arguments)
            if arguments[:2] == ["pr", "merge"]:
                if isinstance(merge_code, Exception):
                    raise merge_code
                return CompletedProcess([], merge_code, "", "main is already used by worktree")
            return CompletedProcess([], api_code, payload if isinstance(payload, str) else json.dumps(payload), "API unavailable")

        snapshot = gate.PullRequestSnapshot("OPEN", "MERGEABLE", HEAD)
        with (
            patch.object(gate, "_run_gh", side_effect=gh),
            patch.object(gate, "_read_pull_request_snapshot", return_value=snapshot),
            patch.object(gate, "_wait_for_status_checks"),
            patch.object(gate, "_read_status_checks"),
            patch.object(gate, "_read_unresolved_review_threads", return_value=[]),
            patch.object(gate, "_require_workflow_runs"),
            redirect_stdout(StringIO()), redirect_stderr(StringIO()),
        ):
            code = gate.main([URL])
        return code, commands

    def merged_payload(self):
        return {"number": 473, "state": "closed", "merged": True,
                "head": {"sha": HEAD}, "merge_commit_sha": MERGE}

    def test_cli_failure_but_confirmed_remote_merge_is_not_repeated(self):
        code, commands = self.invoke(self.merged_payload(), merge_code=1)
        self.assertEqual(0, code)
        self.assertEqual(1, sum(command[:2] == ["pr", "merge"] for command in commands))
        self.assertNotIn("--delete-branch", commands[0])
        self.assertEqual(["api", "--method", "GET", "repos/tomiya7688/AI_game_player/pulls/473"], commands[-1])

    def test_cli_success_also_requires_remote_confirmation(self):
        code, commands = self.invoke(self.merged_payload())
        self.assertEqual(0, code)
        self.assertEqual("api", commands[-1][0])

    def test_rejection_invalid_payload_and_changed_head_are_not_success(self):
        for change in ({"merged": False}, {"merged": 1}, {"state": "open"},
                       {"number": 474}, {"head": {"sha": "c"*40}},
                       {"merge_commit_sha": None}, {"merge_commit_sha": "invalid"}):
            with self.subTest(change=change):
                payload = self.merged_payload() | change
                self.assertEqual(3, self.invoke(payload)[0])
        for payload in (None, [], {}):
            with self.subTest(payload=payload):
                self.assertEqual(3, self.invoke(payload)[0])

    def test_api_failure_does_not_claim_remote_success(self):
        self.assertEqual(3, self.invoke(self.merged_payload(), api_code=1)[0])

    def test_timeout_does_not_repeat_merge_and_can_confirm_committed_result(self):
        code, commands = self.invoke(self.merged_payload(), merge_code=gate.MergeGateError("timeout"))
        self.assertEqual(0, code)
        self.assertEqual(1, sum(command[:2] == ["pr", "merge"] for command in commands))

    def test_malformed_json_is_not_confirmation(self):
        self.assertEqual(3, self.invoke("invalid JSON")[0])

    def test_batch_has_distinct_local_incomplete_exit_without_branch_deletion(self):
        from pathlib import Path
        script = (Path(__file__).resolve().parents[1] / "finish_pr.bat").read_text(encoding="utf-8")
        self.assertIn("if errorlevel 1 goto LOCAL_INCOMPLETE", script)
        self.assertIn(":LOCAL_INCOMPLETE", script)
        self.assertIn("exit /b 4", script)
        self.assertNotIn("git branch -D", script)


if __name__ == "__main__":
    unittest.main()
