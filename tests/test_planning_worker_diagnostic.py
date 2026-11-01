from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.planning_worker_diagnostic import (
    WorkerDiagnostic,
    WorkerPhase,
    collect_worker_diagnostic,
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
