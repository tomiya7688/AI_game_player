"""resource_probes(OS probe)の試験(Issue #787 T15,T16,T23,T27)。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ai_game_player.runtime import resource_probes as probes

# #786 設計書のResourceLifecycleSnapshot v1と同形の固定fixture
SNAPSHOT_V1 = {
    "schema_version": 1,
    "session_id": "s",
    "session_state": "OPEN",
    "current_generation": 3,
    "active_count": 4,
    "releasing_count": 1,
    "pinned_count": 1,
    "active_by_kind": {
        "model_cache": {"count": 1, "known_bytes": 2048, "unknown_count": 0}
    },
    "counters": {"release_failed": 2, "released": 10},
    "last_cleanup": None,
    "max_cleanup_latency_ms": 12.5,
    "recent_failures": [],
}


class ProcessProbeTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_own_process_values(self) -> None:
        values = probes.read_process()
        for key in ("mem.resident_bytes", "mem.private_bytes", "os.handle_count"):
            self.assertIsInstance(values[key], int)
            self.assertGreater(values[key] or 0, 0)
        self.assertIsNotNone(
            values["os.gdi_objects"]
        )  # 0個もあり得るが失敗(None)ではない

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_gdi_object_roundtrip(self) -> None:
        import ctypes

        gdi32 = ctypes.WinDLL("gdi32")
        gdi32.CreateBitmap.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        before = probes.read_process()["os.gdi_objects"]
        bitmap = gdi32.CreateBitmap(1, 1, 1, 1, None)
        try:
            during = probes.read_process()["os.gdi_objects"]
        finally:
            gdi32.DeleteObject(bitmap)
        after = probes.read_process()["os.gdi_objects"]
        self.assertEqual((during or 0) - (before or 0), 1)
        self.assertEqual(after, before)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_handle_count_follows_open_files(self) -> None:
        before = probes.read_process()["os.handle_count"] or 0
        with open(os.devnull, "rb"):
            during = probes.read_process()["os.handle_count"] or 0
        self.assertGreater(during, before)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_linux_values_and_unsupported_gdi(self) -> None:
        values = probes.read_process()
        self.assertGreater(values["mem.resident_bytes"] or 0, 0)
        self.assertGreater(values["os.handle_count"] or 0, 0)
        support = probes.process_support()
        self.assertEqual(
            (support["os.gdi_objects"].status, support["os.gdi_objects"].reason),
            ("unsupported", "platform"),
        )

    def test_unreadable_process_returns_none_not_zero(self) -> None:
        values = probes.read_process(2**31 - 5)  # 存在しないpid
        self.assertTrue(all(v is None for v in values.values()))

    def test_descendants_include_spawned_child(self) -> None:
        child = subprocess.Popen(  # nosemgrep
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            found = {pid for pid, _ in probes.list_descendants(os.getpid())}
            self.assertIn(child.pid, found)
            created = probes.process_creation_time(child.pid)
            self.assertTrue(probes.is_alive(child.pid, created))
            self.assertFalse(
                probes.is_alive(child.pid, created + 1)
            )  # pid再利用は作成時刻で区別
        finally:
            child.kill()
            child.wait()


class TreeGaugeTest(unittest.TestCase):
    def test_tree_sum_skips_vanished_child_and_nones_unreadable_child(self) -> None:
        me = os.getpid()
        reads = {me: {"os.handle_count": 10, "mem.resident_bytes": 100}}

        def reader(pid: int) -> dict[str, int | None]:
            return {**dict.fromkeys(probes.PROCESS_METRICS), **reads.get(pid, {})}

        # 列挙後に終了した子(存在しないpid)は除外され、合計は有効
        vanished = probes.ProcessTreeGauges(me, reader, lambda root: [(2**31 - 7, 1)])
        self.assertEqual(vanished.read()["os.handle_count"], 10)
        # 存在する子の読取に失敗したら、取れた分で合算せずNone
        denied = probes.ProcessTreeGauges(
            me,
            lambda pid: {"os.handle_count": None} if pid == 1 else reader(pid),
            lambda root: [(1, 1)],
        )
        with unittest.mock.patch.object(probes, "_exists", return_value=True):
            self.assertIsNone(denied.read()["os.handle_count"])


class TempGaugeTest(unittest.TestCase):
    def test_counts_files_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a").write_bytes(b"123")
            (Path(root) / "sub").mkdir()
            (Path(root) / "sub" / "b").write_bytes(b"45")
            self.assertEqual(
                probes.TempTreeGauge(root).read(),
                {"temp.file_count": 2, "temp.bytes": 5},
            )

    def test_scan_over_limit_is_none_not_a_partial_count(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            for i in range(5):
                (Path(root) / f"f{i}").write_bytes(b"x")
            gauge = probes.TempTreeGauge(root, max_files=3)
            self.assertEqual(
                gauge.read(), {"temp.file_count": None, "temp.bytes": None}
            )
            self.assertTrue(gauge.truncated)

    def test_symlinks_are_not_followed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as outside,
        ):
            (Path(outside) / "x").write_bytes(b"1")
            try:
                os.symlink(outside, Path(root) / "link", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not permitted")
            self.assertEqual(probes.TempTreeGauge(root).read()["temp.file_count"], 0)


class TempGaugeFailureModesTest(unittest.TestCase):
    def test_missing_root_is_none_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            gauge = probes.TempTreeGauge(Path(root) / "absent")
            self.assertEqual(
                gauge.read(), {"temp.file_count": None, "temp.bytes": None}
            )

    def test_symlink_root_is_not_measured(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as target,
        ):
            (Path(target) / "x").write_bytes(b"1")
            link = Path(root) / "link"
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink not permitted")
            self.assertEqual(probes.TempTreeGauge(link).read()["temp.file_count"], None)

    def test_deadline_is_checked_per_entry(self) -> None:
        ticks = iter(range(1000))
        with tempfile.TemporaryDirectory() as root:
            for i in range(3):
                (Path(root) / f"d{i}").mkdir()
            gauge = probes.TempTreeGauge(
                root, max_seconds=2, clock=lambda: float(next(ticks))
            )
            self.assertIsNone(gauge.read()["temp.file_count"])
            self.assertTrue(gauge.truncated)

    def test_unreadable_subtree_gives_none(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "sub").mkdir()
            real_scandir = os.scandir

            def broken(path: str):  # type: ignore[no-untyped-def]
                if str(path).endswith("sub"):
                    raise PermissionError
                return real_scandir(path)

            with unittest.mock.patch.object(probes.os, "scandir", broken):
                self.assertIsNone(probes.TempTreeGauge(root).read()["temp.bytes"])


class ReparentedDescendantTest(unittest.TestCase):
    def test_seen_descendant_still_alive_is_counted_after_reparenting(self) -> None:
        me = os.getpid()
        gauge = probes.ProcessTreeGauges(
            424242, lambda pid: {"os.handle_count": 5}, lambda root: []
        )
        gauge.seen.add((me, probes.process_creation_time(me)))
        self.assertEqual(gauge.read()["os.handle_count"], 10)


class GpuProbeTest(unittest.TestCase):
    def test_parse_process_vram_sums_target_pids_only(self) -> None:
        output = "10, 100 MiB\n11, 50 MiB\n12, 7 MiB\n"
        self.assertEqual(
            probes.parse_nvidia_compute_apps(output, {10, 12}), 107 * 1024 * 1024
        )

    def test_not_available_is_none(self) -> None:
        self.assertIsNone(probes.parse_nvidia_compute_apps("10, [N/A]\n", {10}))

    def test_tool_missing_and_probe_error(self) -> None:
        def missing(argv: list[str]) -> str:
            raise FileNotFoundError

        def broken(argv: list[str]) -> str:
            raise subprocess.CalledProcessError(1, argv)

        self.assertEqual(
            probes.NvidiaSmiGauge(lambda: {1}, missing)
            .support()["gpu.process_vram_bytes"]
            .reason,
            "tool_missing",
        )
        self.assertEqual(
            probes.NvidiaSmiGauge(lambda: {1}, broken)
            .support()["gpu.process_vram_bytes"]
            .reason,
            "probe_error",
        )

    def test_read_with_fake_runner(self) -> None:
        def fake(argv: list[str]) -> str:
            return (
                "5, 64 MiB\n"
                if "--query-compute-apps=pid,used_memory" in argv
                else "2048\n"
            )

        values = probes.NvidiaSmiGauge(lambda: {5}, fake).read()
        self.assertEqual(values["gpu.process_vram_bytes"], 64 * 1024 * 1024)
        self.assertEqual(values["gpu.device_vram_bytes"], 2048 * 1024 * 1024)


class RegistrySnapshotTest(unittest.TestCase):
    def test_v1_snapshot_maps_to_registry_metrics(self) -> None:
        support, values = probes.RegistrySnapshotGauges().convert(SNAPSHOT_V1)
        self.assertEqual(support.status, "measured")
        self.assertEqual(values["registry.active_leases"], 4)
        self.assertEqual(values["registry.releasing_leases"], 1)
        self.assertEqual(values["registry.failed_leases"], 2)
        self.assertEqual(values["registry.kind.model_cache.bytes"], 2048)
        self.assertEqual(values["registry.max_cleanup_latency_ms"], 12.5)

    def test_unknown_version_or_missing_key_is_unsupported_not_zero(self) -> None:
        gauge = probes.RegistrySnapshotGauges()
        for bad in (
            {**SNAPSHOT_V1, "schema_version": 2},
            {k: v for k, v in SNAPSHOT_V1.items() if k != "counters"},
        ):
            support, values = gauge.convert(bad)
            self.assertEqual(
                (support.status, support.reason, values),
                ("unsupported", "snapshot_version", {}),
            )
        self.assertEqual(gauge.convert(None)[0].reason, "registry_not_connected")

    def test_real_registry_payload_is_accepted(self) -> None:
        from ai_game_player.runtime.resource_lifecycle import SessionResourceRegistry

        registry = SessionResourceRegistry("s")
        support, values = probes.RegistrySnapshotGauges().convert(
            registry.snapshot().to_payload()
        )
        self.assertEqual(
            (support.status, values["registry.active_leases"]), ("measured", 0)
        )


class TracemallocTest(unittest.TestCase):
    def test_heap_is_reported_only_while_tracing(self) -> None:
        gauge = probes.TracemallocGauge()
        self.assertIsNone(gauge.read()["py.heap_traced_bytes"])
        gauge.start()
        try:
            self.assertIsInstance(gauge.read()["py.heap_traced_bytes"], int)
        finally:
            gauge.stop()


if __name__ == "__main__":
    unittest.main()
