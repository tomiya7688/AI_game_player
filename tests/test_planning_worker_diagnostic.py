from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.planning_worker_diagnostic import (
    WorkerDiagnostic,
    WorkerPhase,
    collect_worker_diagnostic,
    new_parent_timing,
    mark_parent_timing,
    record_parent_io_error,
    merge_parent_timing,
)


class WorkerDiagnosticTests(unittest.TestCase):
    def test_fixed_exception_fields_and_first_record_preserve_phase_without_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostic = WorkerDiagnostic()
            diagnostic.mark(WorkerPhase.CHECKPOINT_PUBLISH)
            error = PermissionError(13, "private-model-state-and-path")
            error.winerror = 32
            diagnostic.record_exception(root, error)
            diagnostic.mark(WorkerPhase.FINISHED_PUBLISH)
            diagnostic.record_exception(root, RuntimeError("another-private-value"))
            report = collect_worker_diagnostic(
                root, 1, cleanup_requested=False, exit_seen_before_cleanup=True
            )
            self.assertEqual(report["python_exception_status"], "recorded")
            self.assertEqual(report["python_exception"]["phase"], "checkpoint_publish")
            self.assertEqual(report["python_exception"]["exception_category"], "os")
            self.assertEqual(report["python_exception"]["errno"], 13)
            self.assertEqual(report["python_exception"]["winerror"], 32)
            self.assertGreaterEqual(
                report["python_exception"]["exception_elapsed_ns"],
                report["python_exception"]["phase_elapsed_ns"],
            )
            self.assertNotIn("private", json.dumps(report))
            self.assertLess(len(json.dumps(report)), 2048)

    def test_absent_python_file_distinguishes_abnormal_exit_and_parent_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = collect_worker_diagnostic(
                root, 0xC0000005, cleanup_requested=False, exit_seen_before_cleanup=True
            )
            self.assertEqual(report["python_exception_status"], "absent")
            self.assertEqual(report["failure_classification"], "abnormal_exit")
            self.assertEqual(report["exitcode_signed32"], -1073741819)
            self.assertEqual(report["exitcode_hex"], "0xc0000005")
            self.assertFalse(report["finished_exists"])
            self.assertFalse(report["checkpoint_exists"])
            report = collect_worker_diagnostic(
                root, -9, cleanup_requested=True, exit_seen_before_cleanup=False
            )
            self.assertEqual(report["failure_classification"], "parent_cleanup")
            self.assertEqual(report["exitcode_signed32"], -9)

    def test_untrusted_extra_fields_and_oversized_file_are_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for content in ('{"secret":"private"}', "x" * 1025):
                (root / "python_failure.json").write_text(content)
                report = collect_worker_diagnostic(
                    root, 1, cleanup_requested=False, exit_seen_before_cleanup=True
                )
                self.assertEqual(report["python_exception_status"], "invalid")
                self.assertIsNone(report["python_exception"])
                self.assertNotIn("private", json.dumps(report))

    def test_recording_memory_failure_is_best_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = WorkerDiagnostic()
            with patch(
                "ai_game_player.planning_worker_diagnostic.json.dumps",
                side_effect=MemoryError,
            ):
                diagnostic.record_exception(Path(directory), RuntimeError("private"))
            self.assertFalse((Path(directory) / "python_failure.json").exists())

    def test_phase_timing_failure_is_best_effort(self):
        diagnostic = WorkerDiagnostic()
        with patch(
            "ai_game_player.planning_worker_diagnostic.time.monotonic_ns",
            side_effect=MemoryError,
        ):
            diagnostic.mark(WorkerPhase.CHECKPOINT_PUBLISH)
        self.assertEqual(diagnostic.phase, WorkerPhase.CHECKPOINT_PUBLISH)
        self.assertEqual(diagnostic.phase_elapsed_ns, 0)


class ParentWorkerTimingTests(unittest.TestCase):
    def test_monotonic_fixed_marks_and_io_codes_are_bounded_without_text(self):
        timing = new_parent_timing(100, 500)
        mark_parent_timing(timing, "spawn_started_at_ns", observed_ns=110)
        mark_parent_timing(timing, "spawn_returned_at_ns", observed_ns=120)
        mark_parent_timing(timing, "spawn_started_at_ns", observed_ns=130)
        self.assertEqual(timing["spawn_started_at_ns"], 110)
        for _ in range(3):
            error = PermissionError(13, "private-model-path")
            error.winerror = 5
            record_parent_io_error(timing, "directory_cleanup", error)
        result = merge_parent_timing(None, timing)
        self.assertEqual(len(result["parent_timing"]["io_errors"]), 2)
        self.assertEqual(result["parent_timing"]["io_errors"][0]["winerror"], 5)
        self.assertNotIn("private", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 4096)

    def test_regressed_or_invalid_clock_never_creates_a_timestamp(self):
        for bad in (99, -1, True, float("nan"), 2**100):
            timing = new_parent_timing(100, 500)
            with patch(
                "ai_game_player.planning_worker_diagnostic._parent_clock_ns",
                return_value=bad,
            ):
                mark_parent_timing(timing, "spawn_started_at_ns")
            self.assertTrue(timing["clock_invalid"])
            self.assertNotIn("spawn_started_at_ns", timing)
            self.assertEqual(timing["compute_cutoff_at_ns"], 500)

    def test_invalid_metadata_and_oom_are_noninterfering(self):
        original = {"schema_version": 1}
        timing = new_parent_timing(100, 500)
        with patch(
            "ai_game_player.planning_worker_diagnostic._parent_clock_ns",
            side_effect=MemoryError,
        ):
            mark_parent_timing(timing, "spawn_started_at_ns")
        self.assertNotIn("spawn_started_at_ns", timing)
        for invalid in ({**timing, "io_errors": None}, {**timing, "secret": "private"}):
            self.assertIs(merge_parent_timing(original, invalid), original)
        self.assertIsNone(new_parent_timing(True, 500))
        self.assertIsNone(new_parent_timing(500, 100))

    def test_directory_cleanup_failure_retains_the_original_exception_and_fixed_phase(
        self,
    ):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from ai_game_player.bounded_planner import _planning_directory

        timing = new_parent_timing(0, 500)
        error = PermissionError(13, "private-directory")
        error.winerror = 5
        temporary = SimpleNamespace(
            name="owned-fixture", cleanup=Mock(side_effect=error)
        )
        with patch(
            "ai_game_player.bounded_planner.tempfile.TemporaryDirectory",
            return_value=temporary,
        ):
            with self.assertRaises(PermissionError) as caught:
                with _planning_directory(timing):
                    pass
        self.assertIs(caught.exception, error)
        temporary.cleanup.assert_called_once()
        self.assertEqual(timing["io_errors"][0]["phase"], "directory_cleanup")
        self.assertEqual(timing["io_errors"][0]["errno"], 13)

    def test_timing_oom_cannot_change_real_selection_or_worker_cleanup(self):
        from test_expectimax_planner import ExpectimaxPlannerTests
        from ai_game_player.planning_outcome import PlanningOutcome

        with patch(
            "ai_game_player.planning_worker_diagnostic._parent_clock_ns",
            side_effect=MemoryError,
        ):
            result = ExpectimaxPlannerTests().run_plan()
        self.assertEqual(result.outcome, PlanningOutcome.SELECT)
