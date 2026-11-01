import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_game_player.fail_safe_runtime import (
    AtomicJsonStore,
    FailSafeRuntime,
    FailSafeState,
)


class Clock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 1000 + self.calls / 1000


class CountingStore(AtomicJsonStore):
    def __init__(self, path):
        super().__init__(path)
        self.reads = 0
        self.writes = 0

    def read(self):
        self.reads += 1
        return super().read()

    def write(self, value):
        self.writes += 1
        super().write(value)


class HeartbeatDiagnosticTests(unittest.TestCase):
    def runtime(self, path, enabled=False):
        clock = Clock()
        store = CountingStore(path / "lease.json")
        runtime = FailSafeRuntime(
            path, clock=clock, lease_store=store, external_watchdog=False
        )
        with patch.object(runtime, "_start_heartbeat_locked"):
            runtime.initialize(
                target_pid=4242, target_handle=99, session_id="diagnostic"
            )
        runtime.configure_heartbeat_diagnostics(enabled=enabled)
        return runtime, clock, store

    def test_default_has_no_diagnostic_clock_or_snapshot_work(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, clock, store = self.runtime(Path(directory))
            before = (clock.calls, store.reads, store.writes)
            with patch(
                "ai_game_player.heartbeat_diagnostic._sample_monotonic_ns",
                side_effect=AssertionError,
            ):
                self.assertTrue(runtime.heartbeat())
                self.assertIsNone(runtime.heartbeat_diagnostic_snapshot())
            self.assertEqual(
                (
                    clock.calls - before[0],
                    store.reads - before[1],
                    store.writes - before[2],
                ),
                (2, 1, 1),
            )
            runtime.close()

    def test_opt_in_matches_authority_clock_and_io_counts_and_keeps_last_eight(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline, base_clock, base_store = self.runtime(
                Path(directory) / "baseline"
            )
            traced, clock, store = self.runtime(Path(directory) / "traced", True)
            for _ in range(10):
                self.assertEqual(baseline.heartbeat(), traced.heartbeat())
                self.assertEqual(baseline._lease_expires_at, traced._lease_expires_at)
            before = (clock.calls, store.reads, store.writes)
            snapshot = traced.heartbeat_diagnostic_snapshot()
            self.assertEqual((clock.calls, store.reads, store.writes), before)
            self.assertEqual(
                (clock.calls, store.reads, store.writes),
                (base_clock.calls, base_store.reads, base_store.writes),
            )
            self.assertEqual(
                [r["sequence"] for r in snapshot["completed"]], list(range(3, 11))
            )
            self.assertIsNone(snapshot["in_progress"])
            for row in snapshot["completed"]:
                self.assertEqual(row["result"], "renewed")
                self.assertAlmostEqual(
                    row["lease_expires_unix_seconds"] - row["renewal_unix_seconds"],
                    0.75,
                )
                stamps = [
                    row[k]
                    for k in (
                        "entry_mono_ns",
                        "lock_acquired_mono_ns",
                        "persisted_read_started_mono_ns",
                        "persisted_read_finished_mono_ns",
                        "lease_write_started_mono_ns",
                        "lease_write_finished_mono_ns",
                        "finished_mono_ns",
                    )
                ]
                self.assertEqual(stamps, sorted(stamps))
            self.assertLess(len(json.dumps(snapshot)), 32 * 1024)
            baseline.close()
            traced.close()

    def test_pending_runtime_lock_has_entry_but_no_lock_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, _, _ = self.runtime(Path(directory), True)
            entered = threading.Event()

            def mark_entry():
                entered.set()
                return time.monotonic_ns()

            results = []
            with runtime._lock:
                with patch(
                    "ai_game_player.heartbeat_diagnostic._sample_monotonic_ns",
                    side_effect=mark_entry,
                ):
                    thread = threading.Thread(
                        target=lambda: results.append(runtime.heartbeat())
                    )
                    thread.start()
                    self.assertTrue(entered.wait(1))
                    pending = runtime.heartbeat_diagnostic_snapshot()["in_progress"]
                    self.assertEqual(pending["phase"], "entry")
                    self.assertIsNone(pending["lock_acquired_mono_ns"])
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [True])
            runtime.close()

    def test_snapshot_does_not_wait_for_runtime_lock_and_reports_pending_read(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, _, store = self.runtime(Path(directory), True)
            entered, release = threading.Event(), threading.Event()
            original = store.read

            def blocked():
                entered.set()
                self.assertTrue(release.wait(2))
                return original()

            results = []
            with patch.object(store, "read", blocked):
                thread = threading.Thread(
                    target=lambda: results.append(runtime.heartbeat())
                )
                thread.start()
                try:
                    self.assertTrue(entered.wait(1))
                    began = time.monotonic()
                    snapshot = runtime.heartbeat_diagnostic_snapshot()
                    self.assertLess(time.monotonic() - began, 0.1)
                    pending = snapshot["in_progress"]
                    self.assertEqual(pending["phase"], "persisted_read")
                    self.assertIsNotNone(pending["lock_acquired_mono_ns"])
                    self.assertIsNone(pending["persisted_read_finished_mono_ns"])
                    self.assertEqual(pending["result"], "pending")
                finally:
                    release.set()
                    thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [True])
            runtime.close()

    def test_malformed_snapshot_and_oom_do_not_change_heartbeat_or_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, clock, store = self.runtime(Path(directory), True)
            before = clock.calls
            with patch(
                "ai_game_player.heartbeat_diagnostic._sample_monotonic_ns",
                side_effect=MemoryError,
            ):
                self.assertTrue(runtime.heartbeat())
            self.assertEqual(clock.calls - before, 2)
            self.assertTrue(
                runtime.heartbeat_diagnostic_snapshot()["completed"][-1][
                    "clock_invalid"
                ]
            )
            with patch(
                "ai_game_player.heartbeat_diagnostic.json.dumps",
                side_effect=MemoryError,
            ):
                self.assertIsNone(runtime.heartbeat_diagnostic_snapshot())
            row = runtime._heartbeat_diagnostics._completed[-1]
            row.phase = "private" * 10000
            self.assertIsNone(runtime.heartbeat_diagnostic_snapshot())
            error = PermissionError(13, "private error must remain unchanged")
            with (
                patch.object(store, "read", side_effect=error),
                self.assertRaises(PermissionError) as raised,
            ):
                runtime.heartbeat()
            self.assertIs(raised.exception, error)
            self.assertEqual(runtime.state, FailSafeState.ACTIVE)
            runtime._heartbeat_diagnostics = object()
            self.assertIsNone(runtime.heartbeat_diagnostic_snapshot())
            self.assertTrue(runtime.heartbeat())
            runtime.close()

    def test_clock_regression_is_diagnostic_only_and_does_not_consume_wall_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, clock, _ = self.runtime(Path(directory), True)
            before = clock.calls
            with patch(
                "ai_game_player.heartbeat_diagnostic._sample_monotonic_ns",
                side_effect=[100, 99, 98, 97, 96, 95, 94],
            ):
                self.assertTrue(runtime.heartbeat())
            self.assertEqual(clock.calls - before, 2)
            row = runtime.heartbeat_diagnostic_snapshot()["completed"][-1]
            self.assertTrue(row["clock_invalid"])
            self.assertEqual(row["entry_mono_ns"], 100)
            self.assertIsNone(row["lock_acquired_mono_ns"])
            self.assertEqual(runtime.state, FailSafeState.ACTIVE)
            runtime.close()

    def test_stop_epoch_and_storage_failure_remain_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, clock, store = self.runtime(Path(directory), True)
            epoch = runtime.epoch
            runtime.emergency_stop("user Stop")
            before = clock.calls
            self.assertFalse(runtime.heartbeat())
            self.assertEqual(clock.calls, before)
            self.assertEqual(runtime.epoch, epoch)
            self.assertEqual(runtime.state, FailSafeState.EMERGENCY_STOP)
            self.assertEqual(
                runtime.heartbeat_diagnostic_snapshot()["completed"][-1]["result"],
                "not_active",
            )
            runtime.close()
        with tempfile.TemporaryDirectory() as directory:
            runtime, _, store = self.runtime(Path(directory), True)
            epoch = runtime.epoch
            with patch.object(store, "write", side_effect=OSError("disk")):
                self.assertFalse(runtime.heartbeat())
            self.assertEqual(runtime.epoch, epoch)
            self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
            self.assertEqual(runtime.reason, "heartbeat_storage_failure")
            self.assertEqual(
                runtime.heartbeat_diagnostic_snapshot()["completed"][-1]["result"],
                "storage_failure",
            )
            runtime.close()

    def test_persisted_stop_is_not_rearmed_and_requires_explicit_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, _, store = self.runtime(Path(directory), True)
            state = store.read()
            state.update(state="recovery_required", reason="external_stop")
            store.write(state)
            self.assertFalse(runtime.heartbeat())
            self.assertEqual(runtime.state, FailSafeState.RECOVERY_REQUIRED)
            self.assertEqual(
                runtime.heartbeat_diagnostic_snapshot()["completed"][-1]["result"],
                "persisted_stop",
            )
            with self.assertRaises(ValueError):
                runtime.configure_heartbeat_diagnostics(enabled=1)
            runtime.configure_heartbeat_diagnostics(enabled=False)
            self.assertIsNone(runtime.heartbeat_diagnostic_snapshot())
            runtime.close()
