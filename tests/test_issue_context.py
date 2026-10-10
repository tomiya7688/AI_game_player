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
                    patch.object(context, "fetch_issue", return_value=issue(27, title="[Codex Parent]")) as fetch, \
                    patch.object(context, "fetch_worktree_paths", return_value=[]), \
                    patch.object(context, "load_repository_map", return_value=(None, "map unavailable")):
                self.assertEqual(context.main(["--issue", "27"]), 0)
                listing.assert_not_called()
                fetch.assert_called_once_with(27)
            self.assertIn("#27", output.read_text(encoding="utf-8"))

    def test_only_selected_body_is_fetched(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(context, "OUT", Path(directory) / "pack.md"), \
                    patch.object(context, "fetch_issues", return_value=[issue(5), issue(1, "P0")]), \
                    patch.object(context, "fetch_issue", return_value=issue(1, "P0")) as fetch, \
                    patch.object(context, "fetch_worktree_paths", return_value=[]), \
                    patch.object(context, "load_repository_map", return_value=(None, "map unavailable")):
                self.assertEqual(context.main([]), 0)
                fetch.assert_called_once_with(1)

    def test_context_pack_uses_git_paths_repository_map_and_explained_candidates(self):
        selected = issue(143, title="Update tools/issue_context.py and render_context")
        repository_map = {
            "format": "kadoka-repository-map",
            "schema_version": 1,
            "modules": [
                {
                    "module": "tools.issue_context",
                    "path": "tools/issue_context.py",
                    "symbols": [{"name": "render_context", "qualified_name": "render_context"}],
                },
                {
                    "module": "tests.test_issue_context",
                    "path": "tests/test_issue_context.py",
                    "symbols": [{"name": "IssueContextTests", "qualified_name": "IssueContextTests"}],
                },
                {"module": "tools.unrelated", "path": "tools/unrelated.py", "symbols": []},
                {
                    "module": "tests.test_unrelated",
                    "path": "tests/test_unrelated.py",
                    "symbols": [{"name": "Source", "qualified_name": "Source"}],
                },
            ],
        }
        pack = context.render_context(
            selected,
            max_chars=3000,
            repository_map=repository_map,
            changed_paths=[{"path": "tools/issue_context.py", "status": "M"}],
        )

        self.assertIn("`tools/issue_context.py` — confidence 0.98", pack)
        self.assertIn("working tree marks this path M", pack)
        self.assertIn("symbol(s): render_context", pack)
        self.assertIn("`tools.issue_context.render_context` — confidence 0.84", pack)
        self.assertIn("Test candidate `tests/test_issue_context.py`", pack)
        self.assertIn("Required project check: `finish_task.bat`", pack)
        self.assertIn("not a probability", pack)
        self.assertNotIn("tools/unrelated.py", pack)
        self.assertNotIn("tests/test_unrelated.py", pack)

    def test_git_status_collection_reads_paths_without_file_contents(self):
        with patch.object(
            context.subprocess,
            "check_output",
            return_value=b" M native/tests/c_abi_test.c\0 M tools/issue_context.py\0?? tests/new_fixture.py\0?? .codex/next_issue.md\0",
        ) as command:
            paths = context.fetch_worktree_paths()

        self.assertEqual(
            [
                {"path": "native/tests/c_abi_test.c", "status": "M"},
                {"path": "tests/new_fixture.py", "status": "??"},
                {"path": "tools/issue_context.py", "status": "M"},
            ],
            paths,
        )
        self.assertIn("--porcelain=v1", command.call_args.args[0])
        self.assertIn("-z", command.call_args.args[0])

    def test_natural_language_terms_match_snake_case_module_and_test_paths(self):
        selected = issue(143, title="Improve screen capture reliability")
        repository_map = {
            "format": "kadoka-repository-map",
            "schema_version": 1,
            "modules": [
                {
                    "module": "ai_game_player.screen_capture",
                    "path": "src/ai_game_player/screen_capture.py",
                    "symbols": [],
                },
                {
                    "module": "tests.test_screen_capture",
                    "path": "tests/test_screen_capture.py",
                    "symbols": [],
                },
            ],
        }
        evidence = context.render_repository_evidence(selected, repository_map, [])

        self.assertIn("`src/ai_game_player/screen_capture.py` — confidence 0.50", evidence)
        self.assertIn("Test candidate `tests/test_screen_capture.py`", evidence)

    def test_terminal_symbol_match_uses_map_dependencies_for_descriptive_test_names(self):
        selected = issue(143, title="Review run_and_execute behavior")
        repository_map = {
            "format": "kadoka-repository-map",
            "schema_version": 1,
            "modules": [
                {
                    "module": "ai_game_player.pipeline",
                    "path": "src/ai_game_player/pipeline.py",
                    "dependencies": [],
                    "symbols": [{
                        "name": "run_and_execute",
                        "qualified_name": "DecisionPipeline.run_and_execute",
                    }],
                },
                {
                    "module": "tests.test_pipeline_execute",
                    "path": "tests/test_pipeline_execute.py",
                    "dependencies": ["ai_game_player.pipeline"],
                    "imports": [],
                    "symbols": [],
                },
            ],
        }
        evidence = context.render_repository_evidence(selected, repository_map, [])

        self.assertIn("`src/ai_game_player/pipeline.py` — confidence 0.84", evidence)
        self.assertIn("DecisionPipeline.run_and_execute` — confidence 0.84", evidence)
        self.assertIn("Test candidate `tests/test_pipeline_execute.py` — confidence 0.92", evidence)
        self.assertIn("Repository Map dependency connects this test", evidence)

    def test_exact_test_filename_ranks_above_broad_dependency_matches(self):
        selected = issue(143, title="Improve screen capture reliability")
        repository_map = {
            "format": "kadoka-repository-map",
            "schema_version": 1,
            "modules": [
                {
                    "module": "ai_game_player.screen_capture",
                    "path": "src/ai_game_player/screen_capture.py",
                    "dependencies": [],
                    "imports": [],
                    "symbols": [],
                },
                {
                    "module": "tests.test_bright_region_detector",
                    "path": "tests/test_bright_region_detector.py",
                    "dependencies": ["ai_game_player.screen_capture"],
                    "imports": [],
                    "symbols": [],
                },
                {
                    "module": "tests.test_screen_capture",
                    "path": "tests/test_screen_capture.py",
                    "dependencies": ["ai_game_player.screen_capture"],
                    "imports": [],
                    "symbols": [],
                },
            ],
        }

        modules = context._rank_module_candidates(
            selected,
            repository_map,
            [],
            tests_only=False,
        )
        tests = context._rank_module_candidates(
            selected,
            repository_map,
            [],
            tests_only=True,
            related_sources=modules,
        )

        self.assertEqual("tests/test_screen_capture.py", tests[0]["path"])
        self.assertEqual(0.96, tests[0]["confidence"])

    def test_issue_named_test_ranks_above_broad_dependency_matches(self):
        selected = issue(
            143,
            title="Improve screen capture reliability",
            body="Update `tests/test_screen_capture.py` for the capture flow.",
        )
        source_module = {
            "module": "ai_game_player.screen_capture",
            "path": "src/ai_game_player/screen_capture.py",
        }
        repository_map = {
            "format": "kadoka-repository-map",
            "schema_version": 1,
            "modules": [source_module] + [
                {
                    "module": f"tests.test_{test_name}",
                    "path": f"tests/test_{test_name}.py",
                    "dependencies": ["ai_game_player.screen_capture"],
                    "imports": [],
                    "symbols": [],
                }
                for test_name in (
                    "a_capture_adapter",
                    "b_capture_state",
                    "c_capture_window",
                    "screen_capture",
                )
            ],
        }
        related_sources = context._rank_module_candidates(
            selected, repository_map, [], tests_only=False
        )

        tests = context._rank_module_candidates(
            selected,
            repository_map,
            [],
            tests_only=True,
            related_sources=related_sources,
        )

        self.assertEqual("tests/test_screen_capture.py", tests[0]["path"])
        self.assertEqual(0.99, tests[0]["confidence"])
        self.assertIn("Issue text names this test path", tests[0]["reason"])

    def test_evidence_limit_preserves_test_candidates_and_required_check(self):
        selected = issue(143, title="Improve screen capture reliability")
        modules = []
        for index in range(12):
            modules.extend([
                {
                    "module": f"ai_game_player.screen_capture.feature_{index}",
                    "path": f"src/ai_game_player/screen_capture/feature_{index}.py",
                    "symbols": [{
                        "name": f"ScreenCaptureFeatureHandler{index}",
                        "qualified_name": f"ScreenCaptureFeatureHandler{index}",
                    }],
                },
                {
                    "module": f"tests.test_feature_{index}",
                    "path": f"tests/test_feature_{index}.py",
                    "symbols": [],
                },
            ])
        repository_map = {
            "format": "kadoka-repository-map",
            "schema_version": 1,
            "modules": modules,
        }

        evidence = context.render_repository_evidence(selected, repository_map, [], max_chars=900)

        self.assertLessEqual(len(evidence), 900)
        self.assertIn("## Candidate tests and checks", evidence)
        self.assertIn("Test candidate `tests/test_feature_0.py`", evidence)
        self.assertIn("Required project check: `finish_task.bat`", evidence)

    def test_compact_evidence_keeps_long_candidate_paths_intact(self):
        selected = issue(143, title="Improve screen capture reliability")
        long_test_path = "tests/" + "capture_reliability_details_" * 4 + "screen_capture.py"
        repository_map = {
            "format": "kadoka-repository-map",
            "schema_version": 1,
            "modules": [
                {
                    "module": f"ai_game_player.screen_capture.feature_{index}",
                    "path": f"src/screen_capture/{'long_capture_feature_' * 5}{index}.py",
                    "symbols": [],
                }
                for index in range(4)
            ] + [{
                "module": "tests.test_screen_capture",
                "path": long_test_path,
                "dependencies": ["ai_game_player.screen_capture.feature_0"],
                "symbols": [],
            }],
        }

        evidence = context.render_repository_evidence(selected, repository_map, [], max_chars=500)

        self.assertLessEqual(len(evidence), 500)
        self.assertIn(f"`{long_test_path}`", evidence)
        self.assertIn("finish_task.bat", evidence)
        self.assertNotIn(long_test_path[:70] + "…", evidence)

    def test_minimum_evidence_budget_uses_compact_form(self):
        for map_error in (
            "Repository Map unavailable: file does not exist",
            "Repository Map has an unsupported format or schema version.",
        ):
            with self.subTest(map_error=map_error):
                evidence = context.render_repository_evidence(
                    issue(143, title="Unmatched task"), None, [], map_error=map_error, max_chars=250
                )

                self.assertLessEqual(len(evidence), 250)
                self.assertIn("map unavailable", evidence)
                self.assertIn("finish_task.bat", evidence)

    def test_repository_map_loader_rejects_unknown_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            map_path = Path(directory) / "repo_map.json"
            map_path.write_text(
                json.dumps({"format": "kadoka-repository-map", "schema_version": 1, "modules": []}),
                encoding="utf-8",
            )
            loaded_map, error = context.load_repository_map(map_path)
            self.assertEqual([], loaded_map["modules"])
            self.assertIsNone(error)

            map_path.write_text(
                json.dumps({"format": "kadoka-repository-map", "schema_version": True, "modules": []}),
                encoding="utf-8",
            )
            loaded_map, error = context.load_repository_map(map_path)
            self.assertIsNone(loaded_map)
            self.assertIn("unsupported format or schema version", error)

            map_path.write_text(
                json.dumps({"format": "kadoka-repository-map", "schema_version": 1, "modules": ["bad"]}),
                encoding="utf-8",
            )
            loaded_map, error = context.load_repository_map(map_path)
            self.assertIsNone(loaded_map)
            self.assertIn("unsupported format or schema version", error)

            malformed_modules = [
                {"module": "tools.bad", "path": "tools/bad.py", "symbols": [], "dependencies": [],
                 "imports": ["bad"]},
                {"module": "tools.bad", "path": "tools/bad.py", "symbols": [], "dependencies": [None],
                 "imports": []},
                {"module": "tools.bad", "path": "tools/bad.py", "symbols": [], "dependencies": [],
                 "imports": [{"local_dependencies": [None]}]},
            ]
            for malformed_module in malformed_modules:
                map_path.write_text(
                    json.dumps({
                        "format": "kadoka-repository-map",
                        "schema_version": 1,
                        "modules": [malformed_module],
                    }),
                    encoding="utf-8",
                )
                loaded_map, error = context.load_repository_map(map_path)
                self.assertIsNone(loaded_map)
                self.assertIn("unsupported format or schema version", error)

    def test_malformed_repository_map_refreshes_context_without_map_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            map_path = Path(directory) / "repo_map.json"
            output = Path(directory) / "next_issue.md"
            output.write_text("stale context", encoding="utf-8")
            map_path.write_text(json.dumps({
                "format": "kadoka-repository-map",
                "schema_version": 1,
                "modules": [{
                    "module": "tests.test_issue_context",
                    "path": "tests/test_issue_context.py",
                    "symbols": [],
                    "dependencies": [],
                    "imports": ["bad"],
                }],
            }), encoding="utf-8")
            load_map = context.load_repository_map
            with patch.object(context, "OUT", output), \
                    patch.object(context, "fetch_issue", return_value=issue(143)), \
                    patch.object(context, "fetch_worktree_paths", return_value=[]), \
                    patch.object(context, "load_repository_map", side_effect=lambda: load_map(map_path)):
                self.assertEqual(context.main(["--issue", "143"]), 0)

            refreshed_pack = output.read_text(encoding="utf-8")
            self.assertNotEqual("stale context", refreshed_pack)
            self.assertIn("Repository Map has an unsupported format or schema version", refreshed_pack)
            self.assertNotIn("Test candidate `tests/test_issue_context.py`", refreshed_pack)

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
