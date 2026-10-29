"""測定レポートが古い試行や失敗で成功表示を残さないことを確認します。"""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.runtime_boundary_report import BenchmarkReportAttempt


def _publish_report_in_child(path, published):
    attempt = BenchmarkReportAttempt(Path(path))
    attempt.begin()
    attempt.publish({"schema": "test-report", "value": "newer"})
    published.set()


class BenchmarkReportTest(unittest.TestCase):
    def test_failed_rerun_invalidates_the_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text('{"status":"success","metrics":[1]}', encoding="utf-8")
            attempt = BenchmarkReportAttempt(path)
            attempt.begin()
            self.assertEqual(json.loads(path.read_text())["status"], "running")
            attempt.fail(ValueError("measurement failed"))
            value = json.loads(path.read_text())
            self.assertEqual(value["status"], "failed")
            self.assertEqual(value["run_id"], attempt.run_id)
            self.assertNotIn("metrics", value)

    def test_older_result_and_failure_cannot_replace_a_newer_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            old = BenchmarkReportAttempt(path)
            new = BenchmarkReportAttempt(path)
            old.begin()
            new.begin()
            new.publish({"schema": "test-report", "value": "new"})
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "owns the output"):
                old.publish({"value": "old"})
            self.assertFalse(old.fail(RuntimeError("old failure")))
            self.assertEqual(path.read_bytes(), before)

    def test_real_process_publication_supersedes_older_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            older = BenchmarkReportAttempt(path)
            older.begin()
            context = multiprocessing.get_context("spawn")
            event = context.Event()
            process = context.Process(
                target=_publish_report_in_child, args=(str(path), event)
            )
            process.start()
            try:
                self.assertTrue(event.wait(10), "child did not publish")
                process.join(5)
                self.assertEqual(process.exitcode, 0)
                with self.assertRaises(RuntimeError):
                    older.publish({"value": "old"})
                self.assertFalse(older.fail(ValueError("old failed")))
                value = json.loads(path.read_text())
                self.assertEqual(value["value"], "newer")
                self.assertEqual(value["status"], "success")
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                process.close()

    def test_failed_atomic_replace_keeps_complete_json_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            attempt = BenchmarkReportAttempt(path)
            attempt.begin()
            before = path.read_bytes()
            with mock.patch(
                "ai_game_player.atomic_json.os.replace",
                side_effect=OSError("interrupted replacement"),
            ):
                with self.assertRaises(OSError):
                    attempt.publish({"value": "new"})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(path.read_text())["status"], "running")
            self.assertFalse(list(path.parent.glob(".report.json.*.tmp")))
            attempt.fail(OSError("interrupted replacement"))
            self.assertEqual(json.loads(path.read_text())["status"], "failed")

    def test_transient_read_failure_can_be_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            attempt = BenchmarkReportAttempt(path)
            attempt.begin()
            with mock.patch.object(
                Path, "read_text", side_effect=OSError("temporary read failure")
            ):
                with self.assertRaisesRegex(OSError, "temporary read failure"):
                    attempt.publish({"value": 1})
            self.assertTrue(attempt.fail(OSError("temporary read failure")))
            self.assertEqual(json.loads(path.read_text())["status"], "failed")

    def test_read_failure_followed_by_newer_writer_preserves_newer_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            old = BenchmarkReportAttempt(path)
            old.begin()
            with mock.patch.object(
                Path, "read_text", side_effect=OSError("temporary read failure")
            ):
                with self.assertRaises(OSError):
                    old.publish({"value": "old"})
            new = BenchmarkReportAttempt(path)
            new.begin()
            new.publish({"value": "new"})
            before = path.read_bytes()
            self.assertFalse(old.fail(OSError("old read failure")))
            self.assertEqual(path.read_bytes(), before)

    def test_finished_attempt_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            attempt = BenchmarkReportAttempt(path)
            with self.assertRaises(RuntimeError):
                attempt.publish({})
            attempt.begin()
            attempt.publish({"value": 1})
            self.assertFalse(attempt.fail(ValueError("after success")))
            with self.assertRaises(RuntimeError):
                attempt.begin()
            self.assertEqual(json.loads(path.read_text())["status"], "success")
