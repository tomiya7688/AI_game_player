"""境界測定のIPC失敗時にもworkerと接続が回収されることを確認します。"""

from __future__ import annotations

import multiprocessing
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.runtime_boundary_benchmark import (
    _find_library,
    _load_native,
    benchmark,
    measure_runtime_boundary_cli,
)


from tools.runtime_boundary_ipc import measure_ipc_roundtrip
from tools.runtime_boundary_metrics import measure_operation


def _read_then_wait(connection):
    try:
        connection.recv_bytes()
        time.sleep(60)
    finally:
        connection.close()


class RuntimeBoundaryBenchmarkTest(unittest.TestCase):
    def test_spawned_ipc_worker_roundtrip_succeeds_and_exits(self):
        before = {process.pid for process in multiprocessing.active_children()}
        result = measure_ipc_roundtrip(3)
        self.assertEqual(result["name"], "ipc_pipe_4k_roundtrip")
        self.assertEqual(result["samples"], 3)
        self.assertGreaterEqual(result["p95"], 0)
        self.assertEqual(
            {process.pid for process in multiprocessing.active_children()} - before,
            set(),
        )

    def test_unresponsive_worker_times_out_and_is_reaped(self):
        before = {process.pid for process in multiprocessing.active_children()}
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "response timed out"):
            measure_ipc_roundtrip(1, timeout_seconds=0.2, worker_target=_read_then_wait)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(
            {process.pid for process in multiprocessing.active_children()} - before,
            set(),
        )

    def test_invalid_ipc_timeout_does_not_start_worker(self):
        with mock.patch(
            "tools.runtime_boundary_ipc.multiprocessing.get_context"
        ) as context:
            for timeout in (0, -1, float("inf"), float("nan")):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    measure_ipc_roundtrip(1, timeout_seconds=timeout)
            context.assert_not_called()

    def test_unsupported_abi_is_rejected_before_measurement(self):
        with mock.patch(
            "tools.runtime_boundary_benchmark._load_native",
            return_value=(None, lambda: 999, None),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected Native Runtime ABI"):
                benchmark(Path("not-loaded.dll"), 100)

    def test_multiple_library_matches_require_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one/kadoka_native_runtime.dll"
            second = root / "two/kadoka_native_runtime.dll"
            first.parent.mkdir()
            second.parent.mkdir()
            first.touch()
            second.touch()
            with self.assertRaisesRegex(ValueError, "multiple native libraries"):
                _find_library(None, root)
            self.assertEqual(_find_library(second, root), second.resolve())

    def test_invalid_sample_count_does_not_call_operation(self):
        operation = mock.Mock()
        with self.assertRaises(ValueError):
            measure_operation("invalid", operation, 0)
        operation.assert_not_called()

    def test_cli_reports_failure_without_writing_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            arguments = ["benchmark", "--output", str(report), "--samples", "1"]
            with mock.patch("sys.argv", arguments), mock.patch("sys.stderr"):
                self.assertEqual(measure_runtime_boundary_cli(), 1)
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(value["status"], "failed")
            self.assertNotIn("metrics", value)

    def test_missing_native_exports_are_reported_as_abi_load_failure(self):
        for library in (
            SimpleNamespace(),
            SimpleNamespace(kadoka_runtime_abi_version=lambda: 1),
        ):
            with (
                self.subTest(library=library),
                mock.patch(
                    "tools.runtime_boundary_benchmark.ctypes.CDLL", return_value=library
                ),
            ):
                with self.assertRaisesRegex(ValueError, "C ABI symbol is missing"):
                    _load_native(Path("missing-symbols.dll"))
