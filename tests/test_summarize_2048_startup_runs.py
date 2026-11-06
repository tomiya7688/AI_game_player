"""cohort別の起動診断集計toolの試験。Issue #791 T9。"""

import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest

from tools import summarize_2048_startup_runs as tool


def context(attempt="1", sha="abc", run_id="100", event="pull_request"):
    return {"event": event, "run_id": run_id, "run_attempt": attempt, "sha": sha}


def write_episode(
    root,
    number,
    first_ms,
    *,
    ctx=None,
    version="1.0",
    requestless=0,
    failure_category=None,
):
    folder = Path(root) / f"episode-{number:04d}"
    folder.mkdir(parents=True)
    summary = {
        "schema": "reference-startup-summary/v1",
        "first_board_accepted_ms": first_ms,
        "server_connection_summary": {
            "connections_without_request_line": requestless,
            "connections_awaiting_request_line": 0,
        },
        "browser_version": version,
        "ci_context": ctx if ctx is not None else context(),
    }
    (folder / tool.SUMMARY_NAME).write_text(json.dumps(summary), encoding="utf-8")
    if failure_category is not None:
        failure = {
            "correlation": {
                "ci_context": summary["ci_context"],
                "classification": {"category": failure_category},
            }
        }
        (folder / tool.FAILURE_NAME).write_text(json.dumps(failure), encoding="utf-8")


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def cohort(self, name, times, **kwargs):
        folder = self.root / name
        for index, value in enumerate(times):
            write_episode(folder, index, value, **kwargs)
        return folder

    def test_percentiles_failure_rate_and_categories_per_cohort(self):
        folder = self.cohort("a", [100.0, 200.0, 300.0, 400.0])
        write_episode(folder, 10, None, failure_category="evidence_incomplete")
        write_episode(folder, 11, None, failure_category="connection_without_request")
        result = tool.summarize_cohort("a", folder)
        self.assertEqual(result["episodes"], 6)
        self.assertEqual(result["successful_startups"], 4)
        self.assertEqual(result["startup_failures"], 2)
        self.assertAlmostEqual(result["failure_rate"], 0.3333, places=4)
        self.assertEqual(
            result["first_board_accepted_ms"], {"p50": 200.0, "p95": 400.0}
        )
        self.assertEqual(
            result["failure_categories"],
            {"connection_without_request": 1, "evidence_incomplete": 1},
        )

    def test_failures_are_not_dropped_and_not_in_percentiles(self):
        folder = self.cohort("a", [100.0])
        write_episode(folder, 5, None, failure_category="unclassified")
        result = tool.summarize_cohort("a", folder)
        self.assertEqual(result["first_board_accepted_ms"]["p95"], 100.0)
        self.assertEqual(result["startup_failures"], 1)

    def test_failure_diagnostics_without_a_summary_still_counts_as_a_failure(self):
        folder = self.root / "a" / "episode-0001"
        folder.mkdir(parents=True)
        (folder / tool.FAILURE_NAME).write_text(
            json.dumps({"event": "legacy"}), encoding="utf-8"
        )
        result = tool.summarize_cohort("a", folder.parent)
        self.assertEqual(result["startup_failures"], 1)
        self.assertEqual(result["failure_categories"], {"no_correlation": 1})
        self.assertTrue(result["attempt_unknown"])

    def test_request_less_connections_in_successes_are_distributed(self):
        folder = self.cohort("a", [100.0, 110.0])
        write_episode(folder, 7, 120.0, requestless=2)
        result = tool.summarize_cohort("a", folder)
        self.assertEqual(
            result["request_less_connections_in_successes"], {"0": 2, "2": 1}
        )

    def test_mixed_ci_context_is_rejected(self):
        folder = self.cohort("a", [100.0])
        write_episode(folder, 3, 100.0, ctx=context(sha="other"))
        with self.assertRaises(tool.CohortError):
            tool.summarize_cohort("a", folder)

    def test_retry_only_and_attempt_unknown_are_marked(self):
        retry = self.cohort("retry", [1.0], ctx=context(attempt="2"))
        self.assertTrue(tool.summarize_cohort("retry", retry)["retry_only"])
        local = self.cohort("local", [1.0], ctx={k: None for k in tool.CI_KEYS})
        self.assertTrue(tool.summarize_cohort("local", local)["attempt_unknown"])

    def test_comparison_is_reference_only_for_few_successes_or_version_differences(
        self,
    ):
        small = tool.summarize_cohort("small", self.cohort("small", [1.0] * 3))
        large = tool.summarize_cohort("large", self.cohort("large", [1.0] * 10))
        other = tool.summarize_cohort(
            "other", self.cohort("other", [1.0] * 10, version="2.0")
        )
        same = tool.summarize_cohort("same", self.cohort("same", [2.0] * 10))
        self.assertEqual(
            tool.compare_cohorts(small, large)["comparison"], "reference_only"
        )
        differs = tool.compare_cohorts(large, other)
        self.assertEqual(differs["comparison"], "reference_only")
        self.assertIn("browser_version_differs", differs["reasons"])
        self.assertEqual(tool.compare_cohorts(large, same)["comparison"], "comparable")

    def test_symlinks_and_large_files_are_rejected(self):
        folder = self.cohort("a", [1.0])
        big = folder / "episode-0000" / tool.FAILURE_NAME
        big.write_bytes(b" " * (tool.MAX_FILE_BYTES + 1))
        with self.assertRaises(tool.CohortError):
            tool.summarize_cohort("a", folder)
        with self.assertRaises(tool.CohortError):
            tool.summarize_cohort("missing", self.root / "does-not-exist")

    def test_command_line_never_merges_cohorts_and_refuses_existing_output(self):
        first = self.cohort("a", [100.0])
        second = self.cohort("b", [300.0])
        output = self.root / "out.json"
        code = tool.main(
            [
                "--cohort",
                f"pr={first}",
                "--cohort",
                f"main={second}",
                "--output",
                str(output),
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([c["name"] for c in report["cohorts"]], ["pr", "main"])
        self.assertEqual(report["cohorts"][0]["first_board_accepted_ms"]["p50"], 100.0)
        self.assertEqual(len(report["comparisons"]), 1)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                tool.main(["--cohort", f"pr={first}", "--output", str(output)]), 2
            )
            self.assertEqual(
                tool.main(
                    [
                        "--cohort",
                        "dup=" + str(first),
                        "--cohort",
                        "dup=" + str(second),
                        "--output",
                        str(self.root / "o2.json"),
                    ]
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
