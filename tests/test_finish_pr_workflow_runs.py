import json
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from subprocess import CompletedProcess
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tools import finish_pr_merge
from tools.pr_workflow_evidence import REQUIRED_WORKFLOW_PATHS, WorkflowRunGate, WorkflowEvidenceError


HEAD_SHA = "a" * 40
PR_URL = "https://github.com/tomiya7688/AI_game_player/pull/466"


def completed(payload):
    return CompletedProcess(["gh"], 0, json.dumps(payload), "")


def make_runs(event):
    return [
        {
            "id": (1000 if event == "push" else 2000) + index,
            "run_attempt": 1,
            "head_sha": HEAD_SHA,
            "event": event,
            "path": path,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-18T01:00:00Z",
            "repository": {"full_name": "tomiya7688/AI_game_player"},
            "head_repository": {"full_name": "tomiya7688/AI_game_player"},
            "pull_requests": [{"number": 466, "head": {"sha": HEAD_SHA}}],
        }
        for index, path in enumerate(sorted(REQUIRED_WORKFLOW_PATHS))
    ]


class WorkflowMergeEntryTests(unittest.TestCase):
    def test_successful_check_list_does_not_hide_failed_push_workflow(self):
        def run_gh(arguments, timeout):
            if arguments[:2] == ["pr", "view"]:
                return completed({"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": HEAD_SHA})
            if arguments[:2] == ["pr", "checks"]:
                return completed([{"name": "tests", "state": "SUCCESS", "bucket": "pass"}])
            if arguments[:2] == ["api", "graphql"]:
                return completed({"data": {"repository": {"pullRequest": {"state": "OPEN", "reviewThreads": {
                    "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}
                }}}}})
            if arguments[:2] == ["pr", "merge"]:
                return completed({"merged": True})
            event = parse_qs(urlsplit(arguments[-1]).query)["event"][0]
            runs = make_runs(event)
            if event == "push":
                runs[0]["conclusion"] = "failure"
            return completed({"total_count": len(runs), "workflow_runs": runs})

        with patch.object(finish_pr_merge, "_run_gh", side_effect=run_gh) as gh:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = finish_pr_merge.main([PR_URL])
        self.assertEqual(3, code)
        self.assertFalse(any(call.args[0][:2] == ["pr", "merge"] for call in gh.call_args_list))


class WorkflowRunGateTests(unittest.TestCase):
    def setUp(self):
        self.runs = {event: make_runs(event) for event in ("push", "pull_request")}
        self.pages = {}
        self.requests = []

    def read_json(self, endpoint):
        self.requests.append(endpoint)
        query = parse_qs(urlsplit(endpoint).query)
        self.assertEqual([HEAD_SHA], query["head_sha"])
        self.assertEqual(["100"], query["per_page"])
        event, page = query["event"][0], int(query["page"][0])
        if (event, page) in self.pages:
            return self.pages[event, page]
        runs = self.runs[event]
        return {"total_count": len(runs), "workflow_runs": runs[(page-1)*100:page*100]}

    def verify(self):
        WorkflowRunGate("tomiya7688/AI_game_player", 466, HEAD_SHA, self.read_json).verify()

    def newest(self, conclusion="failure", status="completed"):
        run = deepcopy(self.runs["push"][0])
        run.update(id=9000, created_at="2026-10-18T02:00:00Z", status=status, conclusion=conclusion)
        self.runs["push"].append(run)

    def test_all_fourteen_required_pairs_pass(self):
        self.verify()
        self.assertEqual(2, len(self.requests))

    def test_required_policy_matches_repository_workflows(self):
        root = Path(__file__).resolve().parents[1]
        actual = {path.relative_to(root).as_posix() for pattern in ("*.yml", "*.yaml")
                  for path in (root / ".github/workflows").glob(pattern)}
        self.assertEqual(actual, REQUIRED_WORKFLOW_PATHS)

    def test_missing_event_runs_stop(self):
        self.runs["push"] = []
        with self.assertRaisesRegex(WorkflowEvidenceError, "missing"):
            self.verify()

    def test_missing_required_path_stops(self):
        self.runs["pull_request"].pop()
        with self.assertRaisesRegex(WorkflowEvidenceError, "missing"):
            self.verify()

    def test_newest_failed_run_is_not_hidden_by_previous_success(self):
        self.newest()
        with self.assertRaisesRegex(WorkflowEvidenceError, "9000"):
            self.verify()

    def test_newest_pending_run_is_not_hidden_by_previous_success(self):
        self.newest(None, "in_progress")
        with self.assertRaisesRegex(WorkflowEvidenceError, "in_progress"):
            self.verify()

    def test_newest_success_does_not_reuse_old_failed_run(self):
        self.runs["push"][0]["conclusion"] = "failure"
        self.newest("success")
        self.verify()

    def test_current_rerun_attempt_must_be_successful(self):
        self.runs["push"][0].update(run_attempt=2, conclusion="failure")
        with self.assertRaisesRegex(WorkflowEvidenceError, "attempt 2"):
            self.verify()

    def test_wrong_head_or_event_stops(self):
        for field, value in (("head_sha", "b" * 40), ("event", "push")):
            with self.subTest(field=field):
                self.runs["pull_request"] = make_runs("pull_request")
                self.runs["pull_request"][0][field] = value
                with self.assertRaises(WorkflowEvidenceError):
                    self.verify()

    def test_wrong_or_malformed_repository_stops(self):
        for key in ("repository", "head_repository"):
            for value in (None, {"full_name": "other/repo"}, {"full_name": 5}):
                with self.subTest(key=key, value=value):
                    self.runs["push"] = make_runs("push")
                    self.runs["push"][0][key] = value
                    with self.assertRaises(WorkflowEvidenceError):
                        self.verify()

    def test_unavailable_or_other_pr_association_stops(self):
        for value in (None, [], [{"number": 467, "head": {"sha": HEAD_SHA}}], [{"number": 466, "head": {"sha": "b"*40}}], [{}]):
            with self.subTest(value=value):
                self.runs["pull_request"][0]["pull_requests"] = value
                with self.assertRaises(WorkflowEvidenceError):
                    self.verify()

    def test_duplicate_run_id_stops(self):
        self.runs["push"].append(deepcopy(self.runs["push"][0]))
        with self.assertRaisesRegex(WorkflowEvidenceError, "duplicate"):
            self.verify()

    def test_invalid_run_id_or_attempt_stops(self):
        for key in ("id", "run_attempt"):
            for value in (None, False, 0, -1, "1"):
                with self.subTest(key=key, value=value):
                    self.runs["push"] = make_runs("push")
                    self.runs["push"][0][key] = value
                    with self.assertRaises(WorkflowEvidenceError):
                        self.verify()

    def test_malformed_api_response_stops(self):
        for value in (None, [], {}, {"total_count": False, "workflow_runs": []}, {"total_count": 0, "workflow_runs": {}}, {"total_count": 1, "workflow_runs": [None]}):
            with self.subTest(value=value):
                self.pages["push", 1] = value
                with self.assertRaises(WorkflowEvidenceError):
                    self.verify()

    def test_incomplete_page_stops(self):
        self.pages["push", 1] = {"total_count": 101, "workflow_runs": self.runs["push"]}
        with self.assertRaisesRegex(WorkflowEvidenceError, "incomplete"):
            self.verify()

    def add_pagination_runs(self):
        template = self.runs["push"][0]
        for index in range(96):
            run = deepcopy(template)
            run.update(id=3000+index, path=".github/workflows/extra.yml")
            self.runs["push"].append(run)

    def test_count_change_between_pages_stops(self):
        self.add_pagination_runs()
        self.pages["push", 2] = {"total_count": 104, "workflow_runs": self.runs["push"][100:]}
        with self.assertRaisesRegex(WorkflowEvidenceError, "count changed"):
            self.verify()

    def test_all_pages_are_read(self):
        self.add_pagination_runs()
        self.verify()
        self.assertTrue(any("page=2" in request for request in self.requests))

    def test_api_result_limit_is_not_accepted_as_complete(self):
        self.pages["push", 1] = {"total_count": 1000, "workflow_runs": []}
        with self.assertRaisesRegex(WorkflowEvidenceError, "1000"):
            self.verify()

    def test_invalid_creation_time_stops(self):
        for value in (None, "invalid", "2026-10-18T01:00:00"):
            with self.subTest(value=value):
                self.runs["push"][0]["created_at"] = value
                with self.assertRaisesRegex(WorkflowEvidenceError, "creation time"):
                    self.verify()


if __name__ == "__main__":
    unittest.main()
