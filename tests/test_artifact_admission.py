import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ai_game_player.admission_pending_store import (
    PENDING_DIRECTORY_NAME,
    AdmissionRecordError,
    PendingAdmission,
    PendingAdmissionStore,
)
from ai_game_player.artifact_admission import (
    OUTCOME_COMMITTED,
    OUTCOME_FAILED_PENDING_RETAINED,
    OUTCOME_FAILED_ROLLED_BACK,
    OUTCOME_LOCK_TIMEOUT,
    OUTCOME_REJECTED_CAPACITY,
    OUTCOME_REJECTED_INVALID,
    STATE_INDEXED_BY_OTHER_ENTRY,
    STATE_NO_OBJECT,
    STATE_ORPHAN_CREATED,
    STATE_PREEXISTING,
    STATE_REGISTERED,
    ArtifactAdmissionCoordinator,
    ArtifactAdmissionError,
    ArtifactAdmissionRequest,
)
from ai_game_player.artifact_store import (
    ArtifactIntegrityError,
    FileSystemArtifactStore,
)
from ai_game_player.retention_collector import (
    RETENTION_INDEX_FILENAME,
    LocalRetentionFileSystem,
    RetentionCapacityError,
    RetentionCollector,
)
from ai_game_player.retention_index import RetentionIndexError
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.retention_root_lock import (
    RETENTION_LOCK_FILENAME,
    RetentionLockTimeoutError,
    RetentionRootUnsafeError,
)
from tests.test_retention_root_lock import (
    close_process,
    make_directory_link,
    start_lock_holder,
)

SRC = Path(__file__).parents[1] / "src"
MEDIA_TYPE = "application/octet-stream"
ARTIFACT_TYPE = "frame.raw"


class FlakyFileSystem(LocalRetentionFileSystem):
    """索引書込みとobject削除を、テストの合図で失敗・待機させる。"""

    def __init__(self) -> None:
        self.fail_index = False
        self.fail_remove = False
        self.fail_after_index = False
        self.corrupt_then_fail = False
        self.before_index = None

    def write_index(self, path, document):
        if self.before_index is not None:
            self.before_index()
        if self.fail_index:
            raise OSError("injected index write failure")
        if self.corrupt_then_fail:
            path.write_text("{broken", encoding="utf-8")
            raise OSError("injected failure that left a corrupt index")
        super().write_index(path, document)
        if self.fail_after_index:
            raise OSError("injected failure after the index was published")

    def remove_object(self, path):
        if self.fail_remove:
            raise OSError("injected object removal failure")
        super().remove_object(path)


class PostPublishFailureStore(FileSystemArtifactStore):
    """objectを公開した後で例外を出す（rename後のdirectory fsync失敗に相当）。"""

    def put(self, payload, **kwargs):
        super().put(payload, **kwargs)
        raise OSError("injected failure after publish")


class FailingRemovalRecords(PendingAdmissionStore):
    fail_remove = False

    def remove(self, admission_id):
        if self.fail_remove:
            raise OSError("injected record removal failure")
        super().remove(admission_id)


class FakeClock:
    def __init__(self, now_ms: int = 0) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def payload_of(label: str, size: int) -> bytes:
    return (label.encode("ascii") * size)[:size]


def request_for(
    payload: bytes,
    entry_id: str,
    *,
    with_digest: bool = False,
    protected: bool = False,
    sensitive: bool = False,
) -> ArtifactAdmissionRequest:
    return ArtifactAdmissionRequest(
        payload_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest() if with_digest else None,
        artifact_type=ARTIFACT_TYPE,
        media_type=MEDIA_TYPE,
        entry_id=entry_id,
        protected=protected,
        source_ref="test.source",
        sensitive=sensitive,
    )


def store_payload(payload: bytes):
    return lambda port: port.put(payload)


class AdmissionTestCase(unittest.TestCase):
    soft = 100
    hard = 100

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        self.clock = FakeClock()
        self.events: list = []
        self.coordinator = self.make_coordinator()

    def make_coordinator(
        self,
        *,
        store=None,
        file_system=None,
        policy=None,
        records=None,
        # 並行テストは排他と容量上限を検証する。待ち時間が負荷で尽きると別の失敗になるため長く取る。
        # タイムアウト自体の挙動は LockTimeoutAndTraceTest が短い値を明示して検証する。
        lock_timeout_seconds: float = 60.0,
        trace=True,
    ) -> ArtifactAdmissionCoordinator:
        collector = RetentionCollector(
            store if store is not None else FileSystemArtifactStore(self.root),
            policy if policy is not None else RetentionPolicy(self.soft, self.hard),
            clock=self.clock,
            file_system=file_system,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        return ArtifactAdmissionCoordinator(
            collector,
            trace=self.events.append if trace else None,
            records=records,
        )

    def entries(self, coordinator=None):
        return (coordinator or self.coordinator).collector.load_entries()

    def object_digests(self) -> list[str]:
        return sorted(FileSystemArtifactStore(self.root)._iter_digests())

    def pending_files(self) -> list[str]:
        directory = self.root / PENDING_DIRECTORY_NAME
        return sorted(item.name for item in directory.glob("*.json"))

    def raw_put(self, payload: bytes) -> str:
        """admissionを通さずStoreへ直接putし、未索引objectを作る。"""
        return (
            FileSystemArtifactStore(self.root)
            .put(payload, media_type=MEDIA_TYPE, artifact_type=ARTIFACT_TYPE)
            .sha256
        )

    def digest_with_prefix(self, prefix: str) -> bytes:
        for number in range(100_000):
            candidate = f"probe-{number}".encode("ascii")
            if hashlib.sha256(candidate).hexdigest().startswith(prefix):
                return candidate
        raise AssertionError("no payload with the requested digest prefix")


class AdmitCommitTest(AdmissionTestCase):
    def test_commit_stores_registers_and_clears_pending(self) -> None:
        payload = payload_of("a", 40)
        reference = self.coordinator.admit(
            request_for(payload, "entry-a", protected=True, sensitive=True),
            store_payload(payload),
        )
        self.assertEqual(reference.sha256, hashlib.sha256(payload).hexdigest())
        self.assertTrue(reference.sensitive)
        [entry] = self.entries()
        self.assertEqual(
            (entry.entry_id, entry.artifact_sha256, entry.size_bytes, entry.protected),
            ("entry-a", reference.sha256, 40, True),
        )
        self.assertEqual(self.pending_files(), [])
        self.assertEqual(self.coordinator.inspect_pending(), ())
        self.assertEqual(self.events[-1].outcome, OUTCOME_COMMITTED)

    def test_same_digest_reuses_one_object_for_two_entries(self) -> None:
        payload = payload_of("a", 60)
        for entry_id in ("entry-a", "entry-b"):
            self.coordinator.admit(
                request_for(payload, entry_id, with_digest=True),
                store_payload(payload),
            )
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual({e.entry_id for e in self.entries()}, {"entry-a", "entry-b"})

    def test_existing_digest_needs_no_extra_capacity_at_the_limit(self) -> None:
        payload = payload_of("a", 100)
        self.coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        again = request_for(payload, "entry-b", with_digest=True)
        self.coordinator.admit(again, store_payload(payload))
        self.assertEqual(len(self.entries()), 2)
        # digestを事前に知らせない場合は既存かどうか分からないため、保守的に拒否する。
        with self.assertRaises(RetentionCapacityError):
            self.coordinator.admit(
                request_for(payload, "entry-c"), store_payload(payload)
            )

    def test_unindexed_existing_object_counts_as_zero_when_digest_is_known(
        self,
    ) -> None:
        payload = payload_of("a", 100)
        self.raw_put(payload)
        self.coordinator.admit(
            request_for(payload, "entry-a", with_digest=True), store_payload(payload)
        )
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual(len(self.entries()), 1)


class HardLimitTest(AdmissionTestCase):
    def test_over_limit_is_rejected_before_any_write(self) -> None:
        first = payload_of("a", 90)
        self.coordinator.admit(request_for(first, "entry-a"), store_payload(first))
        called = []
        second = payload_of("b", 20)

        def writer(port):
            called.append(True)
            return port.put(second)

        with self.assertRaises(RetentionCapacityError):
            self.coordinator.admit(request_for(second, "entry-b"), writer)
        self.assertEqual(called, [])
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual(self.pending_files(), [])
        self.assertEqual(self.events[-1].outcome, OUTCOME_REJECTED_CAPACITY)

    def test_unindexed_leftover_counts_toward_the_limit(self) -> None:
        self.raw_put(payload_of("a", 90))
        payload = payload_of("b", 20)
        with self.assertRaises(RetentionCapacityError):
            self.coordinator.admit(
                request_for(payload, "entry-b"), store_payload(payload)
            )

    def test_duplicate_entry_id_is_rejected_before_put(self) -> None:
        payload = payload_of("a", 10)
        self.coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        other = payload_of("b", 10)
        with self.assertRaises(RetentionIndexError):
            self.coordinator.admit(request_for(other, "entry-a"), store_payload(other))
        self.assertEqual(len(self.object_digests()), 1)

    def test_entry_limit_is_enforced_before_put(self) -> None:
        coordinator = self.make_coordinator(
            policy=RetentionPolicy(100, 1_000, max_entries=1)
        )
        first = payload_of("a", 10)
        coordinator.admit(request_for(first, "entry-a"), store_payload(first))
        second = payload_of("b", 10)
        with self.assertRaises(RetentionCapacityError):
            coordinator.admit(request_for(second, "entry-b"), store_payload(second))
        self.assertEqual(len(self.object_digests()), 1)


class ConcurrencyTest(AdmissionTestCase):
    def run_in_threads(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results: list = [None] * len(jobs)

        def run(index, job):
            barrier.wait(timeout=10)
            try:
                results[index] = job()
            except BaseException as error:  # noqa: BLE001 - 結果として検査する
                results[index] = error

        threads = [
            threading.Thread(target=run, args=(index, job))
            for index, job in enumerate(jobs)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive())
        return results

    def test_two_writers_never_exceed_the_hard_limit(self) -> None:
        first, second = payload_of("a", 60), payload_of("b", 60)
        left, right = self.make_coordinator(), self.make_coordinator()
        results = self.run_in_threads(
            [
                lambda: left.admit(request_for(first, "entry-a"), store_payload(first)),
                lambda: right.admit(
                    request_for(second, "entry-b"), store_payload(second)
                ),
            ]
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RetentionCapacityError)
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.coordinator.inspect_pending(), ())

    def test_many_writers_keep_total_within_limit_without_orphans(self) -> None:
        payloads = [payload_of(chr(ord("a") + index), 30) for index in range(8)]
        coordinators = [self.make_coordinator() for _ in payloads]
        jobs = [
            (
                lambda c=c, p=p, i=i: c.admit(
                    request_for(p, f"entry-{i}"), store_payload(p)
                )
            )
            for i, (c, p) in enumerate(zip(coordinators, payloads))
        ]
        results = self.run_in_threads(jobs)
        succeeded = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual(len(succeeded), 3, [repr(r) for r in results])
        self.assertTrue(
            all(
                isinstance(r, RetentionCapacityError)
                for r in results
                if r not in succeeded
            ),
            [repr(r) for r in results],
        )
        self.assertEqual(len(self.object_digests()), 3)
        self.assertEqual(len(self.entries()), 3)

    def test_same_digest_concurrently_reuses_one_object(self) -> None:
        payload = payload_of("a", 100)
        left, right = self.make_coordinator(), self.make_coordinator()
        results = self.run_in_threads(
            [
                lambda: left.admit(
                    request_for(payload, "entry-a", with_digest=True),
                    store_payload(payload),
                ),
                lambda: right.admit(
                    request_for(payload, "entry-b", with_digest=True),
                    store_payload(payload),
                ),
            ]
        )
        self.assertFalse(any(isinstance(r, BaseException) for r in results), results)
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual(len(self.entries()), 2)

    def test_admit_waits_for_a_collect_in_progress(self) -> None:
        policy = RetentionPolicy(100, 100, default_ttl_ms=10)
        gated = FlakyFileSystem()
        collecting = self.make_coordinator(policy=policy, file_system=gated)
        old = payload_of("a", 60)
        collecting.admit(request_for(old, "entry-old"), store_payload(old))
        self.clock.now_ms = 1_000
        in_index_write, release = threading.Event(), threading.Event()
        gated.before_index = lambda: (in_index_write.set(), release.wait(10))
        collect_thread = threading.Thread(target=collecting.collector.collect)
        collect_thread.start()
        self.assertTrue(in_index_write.wait(10))

        admitter = self.make_coordinator(policy=policy)
        new = payload_of("b", 80)
        admit_results: list = []
        admit_thread = threading.Thread(
            target=lambda: admit_results.append(
                admitter.admit(request_for(new, "entry-new"), store_payload(new))
            )
        )
        admit_thread.start()
        time.sleep(0.4)
        self.assertTrue(admit_thread.is_alive(), "admit must wait for the collect")
        release.set()
        collect_thread.join(timeout=30)
        admit_thread.join(timeout=30)
        self.assertEqual(len(admit_results), 1)
        # collect完了後に再計測されるため、期限切れの旧objectは容量へ残らず80byteが入る。
        self.assertEqual([e.entry_id for e in self.entries()], ["entry-new"])
        self.assertEqual(len(self.object_digests()), 1)

    def test_two_processes_admit_without_exceeding_the_limit(self) -> None:
        script = """
import os, sys, time
from pathlib import Path
from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.retention_collector import RetentionCollector, RetentionCapacityError
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.artifact_admission import ArtifactAdmissionCoordinator, ArtifactAdmissionRequest
root, label, ready, go = sys.argv[1:5]
payload = (label.encode() * 60)[:60]
coordinator = ArtifactAdmissionCoordinator(RetentionCollector(FileSystemArtifactStore(Path(root)), RetentionPolicy(100, 100)))
Path(ready).write_text("ready")
while not Path(go).exists():
    time.sleep(0.002)
request = ArtifactAdmissionRequest(60, None, "frame.raw", "application/octet-stream", "entry-" + label, False, "proc." + label)
try:
    coordinator.admit(request, lambda port: port.put(payload))
    print("admitted")
except RetentionCapacityError:
    print("rejected")
"""
        go = Path(self.temp.name) / "go"
        environment = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1")
        children = []
        for label in ("a", "b"):
            ready = Path(self.temp.name) / f"ready-{label}"
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(self.root),
                    label,
                    str(ready),
                    str(go),
                ],
                stdout=subprocess.PIPE,
                text=True,
                env=environment,
                cwd=self.temp.name,
            )
            self.addCleanup(close_process, child)
            children.append((child, ready))
        deadline = time.monotonic() + 30
        while not all(ready.exists() for _, ready in children):
            self.assertLess(time.monotonic(), deadline, "children did not become ready")
            time.sleep(0.01)
        go.write_text("go")
        outputs = []
        for child, _ in children:
            outputs.append(child.communicate(timeout=60)[0].strip())
            self.assertEqual(child.returncode, 0)
        self.assertEqual(sorted(outputs), ["admitted", "rejected"])
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual(len(self.entries()), 1)


class FailureCompensationTest(AdmissionTestCase):
    def test_writer_failure_before_put_changes_nothing(self) -> None:
        payload = payload_of("a", 10)

        def writer(port):
            raise OSError("writer failed before put")

        with self.assertRaises(ArtifactAdmissionError) as caught:
            self.coordinator.admit(request_for(payload, "entry-a"), writer)
        self.assertTrue(caught.exception.rolled_back)
        self.assertFalse(caught.exception.pending_retained)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(self.object_digests(), [])
        self.assertEqual(self.pending_files(), [])
        self.assertEqual(self.events[-1].outcome, OUTCOME_FAILED_ROLLED_BACK)

    def test_failure_after_publish_removes_the_new_object(self) -> None:
        store = PostPublishFailureStore(self.root)
        coordinator = self.make_coordinator(store=store)
        payload = payload_of("a", 10)
        with self.assertRaises(ArtifactAdmissionError) as caught:
            coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        self.assertTrue(caught.exception.rolled_back)
        self.assertEqual(self.object_digests(), [])
        self.assertEqual(self.pending_files(), [])
        self.assertEqual(coordinator.stats.rollbacks, 1)

    def test_register_failure_rolls_back_and_is_not_silent(self) -> None:
        file_system = FlakyFileSystem()
        coordinator = self.make_coordinator(file_system=file_system)
        kept = payload_of("a", 20)
        coordinator.admit(request_for(kept, "entry-kept"), store_payload(kept))
        file_system.fail_index = True
        payload = payload_of("b", 20)
        with self.assertRaises(ArtifactAdmissionError) as caught:
            coordinator.admit(request_for(payload, "entry-new"), store_payload(payload))
        self.assertTrue(caught.exception.rolled_back)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(
            [e.entry_id for e in self.entries(coordinator)], ["entry-kept"]
        )
        self.assertEqual(self.object_digests(), [hashlib.sha256(kept).hexdigest()])
        self.assertEqual(self.pending_files(), [])

    def test_exception_after_the_index_was_published_is_a_success(self) -> None:
        file_system = FlakyFileSystem()
        file_system.fail_after_index = True
        coordinator = self.make_coordinator(file_system=file_system)
        payload = payload_of("a", 20)
        reference = coordinator.admit(
            request_for(payload, "entry-a"), store_payload(payload)
        )
        self.assertEqual(reference.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual([e.entry_id for e in self.entries(coordinator)], ["entry-a"])
        self.assertEqual(self.object_digests(), [reference.sha256])
        self.assertEqual(self.pending_files(), [])
        self.assertEqual(self.events[-1].outcome, OUTCOME_COMMITTED)

    def test_unreadable_index_after_a_failed_register_keeps_object_and_record(
        self,
    ) -> None:
        file_system = FlakyFileSystem()
        file_system.corrupt_then_fail = True
        coordinator = self.make_coordinator(file_system=file_system)
        payload = payload_of("a", 20)
        with self.assertRaises(ArtifactAdmissionError) as caught:
            coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        self.assertTrue(caught.exception.pending_retained)
        self.assertEqual(self.object_digests(), [hashlib.sha256(payload).hexdigest()])
        self.assertEqual(len(self.pending_files()), 1)
        self.assertEqual(self.events[-1].outcome, OUTCOME_FAILED_PENDING_RETAINED)

    def test_index_write_failure_leaves_the_previous_index_untouched(self) -> None:
        file_system = FlakyFileSystem()
        coordinator = self.make_coordinator(file_system=file_system)
        first = payload_of("a", 20)
        coordinator.admit(request_for(first, "entry-a"), store_payload(first))
        before = (self.root / RETENTION_INDEX_FILENAME).read_bytes()
        file_system.fail_index = True
        second = payload_of("b", 20)
        with self.assertRaises(ArtifactAdmissionError):
            coordinator.admit(request_for(second, "entry-b"), store_payload(second))
        self.assertEqual((self.root / RETENTION_INDEX_FILENAME).read_bytes(), before)

    def test_preexisting_indexed_object_survives_a_failed_second_admission(
        self,
    ) -> None:
        file_system = FlakyFileSystem()
        coordinator = self.make_coordinator(file_system=file_system)
        payload = payload_of("a", 30)
        coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        file_system.fail_index = True
        with self.assertRaises(ArtifactAdmissionError) as caught:
            coordinator.admit(
                request_for(payload, "entry-b", with_digest=True),
                store_payload(payload),
            )
        self.assertTrue(caught.exception.rolled_back)
        self.assertEqual(self.object_digests(), [hashlib.sha256(payload).hexdigest()])
        self.assertEqual([e.entry_id for e in self.entries(coordinator)], ["entry-a"])

    def test_preexisting_unindexed_object_is_not_deleted_by_rollback(self) -> None:
        file_system = FlakyFileSystem()
        coordinator = self.make_coordinator(file_system=file_system)
        payload = payload_of("a", 30)
        digest = self.raw_put(payload)
        file_system.fail_index = True
        with self.assertRaises(ArtifactAdmissionError) as caught:
            coordinator.admit(
                request_for(payload, "entry-a", with_digest=True),
                store_payload(payload),
            )
        self.assertTrue(caught.exception.rolled_back)
        self.assertEqual(self.object_digests(), [digest])
        self.assertEqual(self.pending_files(), [])

    def test_keyboard_interrupt_is_compensated_and_reraised_unwrapped(self) -> None:
        payload = payload_of("a", 30)

        def writer(port):
            port.put(payload)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.coordinator.admit(request_for(payload, "entry-a"), writer)
        self.assertEqual(self.object_digests(), [])
        self.assertEqual(self.pending_files(), [])
        self.assertEqual(self.events[-1].outcome, OUTCOME_FAILED_ROLLED_BACK)
        self.assertEqual(len(self.events), 1)

    def test_rollback_failure_retains_pending_record_for_recovery(self) -> None:
        file_system = FlakyFileSystem()
        coordinator = self.make_coordinator(file_system=file_system)
        payload = payload_of("a", 30)
        digest = hashlib.sha256(payload).hexdigest()
        file_system.fail_index = True
        file_system.fail_remove = True
        with self.assertRaises(ArtifactAdmissionError) as caught:
            coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        self.assertFalse(caught.exception.rolled_back)
        self.assertTrue(caught.exception.pending_retained)
        self.assertEqual(self.events[-1].outcome, OUTCOME_FAILED_PENDING_RETAINED)
        self.assertEqual(len(self.pending_files()), 1)
        self.assertEqual(self.object_digests(), [digest])

        [status] = coordinator.inspect_pending()
        self.assertEqual(status.state, STATE_ORPHAN_CREATED)
        self.assertEqual(status.record.entry_id, "entry-a")

        file_system.fail_index = False
        file_system.fail_remove = False
        report = coordinator.recover_pending()
        self.assertEqual(report.deleted_objects, (digest,))
        self.assertEqual(self.object_digests(), [])
        self.assertEqual(self.pending_files(), [])

    def test_cleanup_failure_after_success_does_not_fail_the_admission(self) -> None:
        records = FailingRemovalRecords(self.root)
        coordinator = self.make_coordinator(records=records)
        records.fail_remove = True
        payload = payload_of("a", 30)
        coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        self.assertTrue(self.events[-1].pending_cleanup_failed)
        self.assertEqual(coordinator.stats.pending_cleanup_failures, 1)
        [status] = coordinator.inspect_pending()
        self.assertEqual(status.state, STATE_REGISTERED)
        records.fail_remove = False
        report = coordinator.recover_pending()
        self.assertEqual(len(report.removed_records), 1)
        self.assertEqual(report.deleted_objects, ())
        self.assertEqual(len(self.object_digests()), 1)


class PendingRecoveryTest(AdmissionTestCase):
    def make_record(self, payload: bytes, **overrides) -> PendingAdmission:
        values = dict(
            admission_id="a" * 32,
            artifact_sha256=hashlib.sha256(payload).hexdigest(),
            created_object=True,
            entry_id="entry-x",
            source_ref="crash.fixture",
            artifact_type=ARTIFACT_TYPE,
            media_type=MEDIA_TYPE,
            payload_size=len(payload),
            protected=False,
            started_at_ms=1,
            owner_pid=1,
        )
        values.update(overrides)
        return PendingAdmission(**values)

    def test_crashed_process_leaves_a_recoverable_record_and_releases_the_lock(
        self,
    ) -> None:
        script = """
import os, sys
from pathlib import Path
from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.retention_collector import RetentionCollector
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.artifact_admission import ArtifactAdmissionCoordinator, ArtifactAdmissionRequest
payload = (b"c" * 40)
coordinator = ArtifactAdmissionCoordinator(RetentionCollector(FileSystemArtifactStore(Path(sys.argv[1])), RetentionPolicy(100, 100)))
request = ArtifactAdmissionRequest(40, None, "frame.raw", "application/octet-stream", "entry-crash", False, "crash.child")
def writer(port):
    port.put(payload)
    os._exit(3)
coordinator.admit(request, writer)
"""
        environment = dict(os.environ, PYTHONPATH=str(SRC), PYTHONUTF8="1")
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            env=environment,
            cwd=self.temp.name,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 3)
        [status] = self.coordinator.inspect_pending()
        self.assertEqual(status.state, STATE_ORPHAN_CREATED)
        self.assertEqual(status.record.source_ref, "crash.child")
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual(self.entries(), ())

        # 残骸(40byte)は容量に数えられる。lockはprocess終了で解放済みなので別のadmissionが進める。
        big = payload_of("d", 70)
        with self.assertRaises(RetentionCapacityError):
            self.coordinator.admit(request_for(big, "entry-big"), store_payload(big))
        small = payload_of("e", 50)
        self.coordinator.admit(request_for(small, "entry-small"), store_payload(small))

        report = self.coordinator.recover_pending()
        self.assertEqual(len(report.deleted_objects), 1)
        self.assertEqual(self.coordinator.inspect_pending(), ())
        self.assertEqual(len(self.object_digests()), 1)
        self.assertEqual([e.entry_id for e in self.entries()], ["entry-small"])

    def test_states_are_classified_against_the_real_store_and_index(self) -> None:
        registered = payload_of("a", 10)
        self.coordinator.admit(
            request_for(registered, "entry-reg"), store_payload(registered)
        )
        other_entry = payload_of("b", 10)
        self.coordinator.admit(
            request_for(other_entry, "entry-other"), store_payload(other_entry)
        )
        preexisting = payload_of("c", 10)
        self.raw_put(preexisting)
        orphan = payload_of("d", 10)
        self.raw_put(orphan)
        missing = payload_of("e", 10)

        records = PendingAdmissionStore(self.root)
        cases = {
            "1" * 32: (registered, dict(entry_id="entry-reg"), STATE_REGISTERED),
            "2" * 32: (
                other_entry,
                dict(entry_id="entry-missing"),
                STATE_INDEXED_BY_OTHER_ENTRY,
            ),
            "3" * 32: (preexisting, dict(created_object=False), STATE_PREEXISTING),
            "4" * 32: (orphan, dict(created_object=True), STATE_ORPHAN_CREATED),
            "5" * 32: (missing, dict(created_object=True), STATE_NO_OBJECT),
        }
        for admission_id, (payload, overrides, _) in cases.items():
            records.write(
                self.make_record(payload, admission_id=admission_id, **overrides)
            )
        states = {
            s.record.admission_id: s.state for s in self.coordinator.inspect_pending()
        }
        self.assertEqual(states, {key: value[2] for key, value in cases.items()})

        report = self.coordinator.recover_pending()
        self.assertEqual(len(report.removed_records), 5)
        self.assertEqual(report.deleted_objects, (hashlib.sha256(orphan).hexdigest(),))
        remaining = set(self.object_digests())
        for kept in (registered, other_entry, preexisting):
            self.assertIn(hashlib.sha256(kept).hexdigest(), remaining)
        self.assertNotIn(hashlib.sha256(orphan).hexdigest(), remaining)
        self.assertEqual(self.coordinator.inspect_pending(), ())

    def test_orphan_referenced_by_another_record_is_retained(self) -> None:
        payload = payload_of("a", 10)
        self.raw_put(payload)
        records = PendingAdmissionStore(self.root)
        records.write(
            self.make_record(payload, admission_id="1" * 32, entry_id="entry-a")
        )
        records.write(
            self.make_record(payload, admission_id="2" * 32, entry_id="entry-b")
        )
        report = self.coordinator.recover_pending()
        self.assertEqual(report.deleted_objects, ())
        self.assertEqual(report.retained_records, ("1" * 32, "2" * 32))
        self.assertEqual(len(self.object_digests()), 1)

    def test_rollback_keeps_object_when_another_record_references_it(self) -> None:
        payload = payload_of("a", 10)
        stale = self.make_record(
            payload, admission_id="9" * 32, entry_id="entry-stale", created_object=False
        )
        PendingAdmissionStore(self.root).write(stale)
        file_system = FlakyFileSystem()
        coordinator = self.make_coordinator(file_system=file_system)
        file_system.fail_index = True
        with self.assertRaises(ArtifactAdmissionError) as caught:
            coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        self.assertTrue(caught.exception.pending_retained)
        self.assertEqual(len(self.pending_files()), 2)
        self.assertEqual(len(self.object_digests()), 1)
        file_system.fail_index = False
        coordinator.recover_pending()
        self.assertEqual(self.object_digests(), [])
        self.assertEqual(self.pending_files(), [])

    def test_corrupt_record_makes_inspection_fail_closed(self) -> None:
        directory = self.root / PENDING_DIRECTORY_NAME
        directory.mkdir(parents=True)
        (directory / f"{'a' * 32}.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(AdmissionRecordError):
            self.coordinator.inspect_pending()
        with self.assertRaises(AdmissionRecordError):
            self.coordinator.recover_pending()
        record = json.loads(json.dumps(self.make_record(b"x").to_dict()))
        record["unknown"] = 1
        (directory / f"{'a' * 32}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        with self.assertRaises(AdmissionRecordError):
            self.coordinator.inspect_pending()

    def test_record_whose_name_and_id_disagree_is_rejected(self) -> None:
        records = PendingAdmissionStore(self.root)
        records.write(self.make_record(b"x", admission_id="a" * 32))
        directory = self.root / PENDING_DIRECTORY_NAME
        (directory / f"{'a' * 32}.json").rename(directory / f"{'b' * 32}.json")
        with self.assertRaises(AdmissionRecordError):
            records.list_records()


class SafetyTest(AdmissionTestCase):
    def test_corrupt_index_rejects_without_calling_the_writer(self) -> None:
        self.root.mkdir()
        (self.root / RETENTION_INDEX_FILENAME).write_text("{broken", encoding="utf-8")
        called = []
        payload = payload_of("a", 10)

        def writer(port):
            called.append(True)
            return port.put(payload)

        with self.assertRaises(RetentionIndexError):
            self.coordinator.admit(request_for(payload, "entry-a"), writer)
        self.assertEqual(called, [])
        self.assertEqual(self.object_digests(), [])
        self.assertEqual(self.events[-1].outcome, OUTCOME_REJECTED_INVALID)

    def test_corrupt_object_metadata_rejects_fail_closed(self) -> None:
        digest = self.raw_put(payload_of("a", 10))
        (self.root / digest[:2] / digest / "metadata.json").write_text(
            "{}", encoding="utf-8"
        )
        payload = payload_of("b", 10)
        with self.assertRaises(RetentionIndexError):
            self.coordinator.admit(
                request_for(payload, "entry-b"), store_payload(payload)
            )
        self.assertEqual(self.object_digests(), [digest])

    def test_root_link_is_rejected(self) -> None:
        real = Path(self.temp.name) / "real"
        real.mkdir()
        link = Path(self.temp.name) / "linked-store"
        make_directory_link(link, real)
        coordinator = self.make_coordinator(store=FileSystemArtifactStore(link))
        payload = payload_of("a", 10)
        with self.assertRaises(RetentionRootUnsafeError):
            coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        self.assertEqual(list(real.iterdir()), [])

    def test_lock_file_symlink_is_rejected(self) -> None:
        self.root.mkdir()
        outside = Path(self.temp.name) / "outside.bin"
        outside.write_bytes(b"x")
        try:
            os.symlink(outside, self.root / RETENTION_LOCK_FILENAME)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"cannot create symlink: {error}")
        payload = payload_of("a", 10)
        with self.assertRaises(RetentionRootUnsafeError):
            self.coordinator.admit(
                request_for(payload, "entry-a"), store_payload(payload)
            )
        self.assertEqual(outside.read_bytes(), b"x")

    def test_shard_directory_link_is_rejected_and_nothing_is_written_outside(
        self,
    ) -> None:
        payload = self.digest_with_prefix("ab")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.root.mkdir()
        make_directory_link(self.root / "ab", outside)
        with self.assertRaises(ArtifactAdmissionError) as caught:
            self.coordinator.admit(
                request_for(payload, "entry-a"), store_payload(payload)
            )
        self.assertIsInstance(caught.exception.__cause__, RetentionRootUnsafeError)
        self.assertEqual(list(outside.iterdir()), [])

    def test_pending_directory_link_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.root.mkdir()
        make_directory_link(self.root / PENDING_DIRECTORY_NAME, outside)
        payload = payload_of("a", 10)
        with self.assertRaises(ArtifactAdmissionError) as caught:
            self.coordinator.admit(
                request_for(payload, "entry-a"), store_payload(payload)
            )
        self.assertIsInstance(caught.exception.__cause__, AdmissionRecordError)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.object_digests(), [])

    def test_payload_size_mismatch_is_rejected_before_put(self) -> None:
        payload = payload_of("a", 10)
        request = ArtifactAdmissionRequest(
            11, None, ARTIFACT_TYPE, MEDIA_TYPE, "entry-a", False, "test.source"
        )
        with self.assertRaises(ArtifactAdmissionError) as caught:
            self.coordinator.admit(request, store_payload(payload))
        self.assertIsInstance(caught.exception.__cause__, ArtifactIntegrityError)
        self.assertEqual(self.object_digests(), [])
        self.assertEqual(self.entries(), ())

    def test_payload_digest_mismatch_is_rejected_before_put(self) -> None:
        payload = payload_of("a", 10)
        wrong = request_for(payload_of("b", 10), "entry-a", with_digest=True)
        with self.assertRaises(ArtifactAdmissionError) as caught:
            self.coordinator.admit(wrong, store_payload(payload))
        self.assertIsInstance(caught.exception.__cause__, ArtifactIntegrityError)
        self.assertEqual(self.object_digests(), [])

    def test_port_allows_one_put_and_exposes_no_store_or_path(self) -> None:
        payload = payload_of("a", 10)

        def writer(port):
            reference = port.put(payload)
            for forbidden in ("store", "root", "path", "collector", "__dict__"):
                self.assertFalse(hasattr(port, forbidden), forbidden)
            port.put(payload)
            return reference

        with self.assertRaises(ArtifactAdmissionError) as caught:
            self.coordinator.admit(request_for(payload, "entry-a"), writer)
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertEqual(self.object_digests(), [])

    def test_writer_returning_without_using_the_port_is_rejected(self) -> None:
        payload = payload_of("a", 10)
        foreign = FileSystemArtifactStore(Path(self.temp.name) / "foreign").put(
            payload, media_type=MEDIA_TYPE, artifact_type=ARTIFACT_TYPE
        )
        with self.assertRaises(ArtifactAdmissionError):
            self.coordinator.admit(
                request_for(payload, "entry-a"), lambda port: foreign
            )
        self.assertEqual(self.entries(), ())

    def test_writer_must_return_the_reference_it_stored(self) -> None:
        payload = payload_of("a", 10)
        other = payload_of("b", 10)
        foreign = FileSystemArtifactStore(Path(self.temp.name) / "foreign").put(
            other, media_type=MEDIA_TYPE, artifact_type=ARTIFACT_TYPE
        )

        def writer(port):
            port.put(payload)
            return foreign

        with self.assertRaises(ArtifactAdmissionError) as caught:
            self.coordinator.admit(request_for(payload, "entry-a"), writer)
        self.assertTrue(caught.exception.rolled_back)
        self.assertEqual(self.object_digests(), [])


class LockTimeoutAndTraceTest(AdmissionTestCase):
    def test_lock_contention_fails_boundedly_without_a_transaction(self) -> None:
        holder = start_lock_holder(self.root)
        self.addCleanup(close_process, holder)
        coordinator = self.make_coordinator(lock_timeout_seconds=0.3)
        payload = payload_of("a", 10)
        called = []
        started = time.monotonic()
        with self.assertRaises(RetentionLockTimeoutError):
            coordinator.admit(
                request_for(payload, "entry-a"),
                lambda port: (called.append(True), port.put(payload))[1],
            )
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(called, [])
        self.assertEqual(self.events[-1].outcome, OUTCOME_LOCK_TIMEOUT)
        self.assertEqual(self.object_digests(), [])
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
        coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))

    def test_trace_reports_lock_wait_transaction_time_and_rollbacks(self) -> None:
        file_system = FlakyFileSystem()
        coordinator = self.make_coordinator(file_system=file_system)
        first = payload_of("a", 10)
        coordinator.admit(request_for(first, "entry-a"), store_payload(first))
        file_system.fail_index = True
        second = payload_of("b", 10)
        with self.assertRaises(ArtifactAdmissionError):
            coordinator.admit(request_for(second, "entry-b"), store_payload(second))
        committed, failed = self.events
        self.assertEqual(committed.outcome, OUTCOME_COMMITTED)
        self.assertEqual(failed.outcome, OUTCOME_FAILED_ROLLED_BACK)
        self.assertTrue(failed.rolled_back)
        self.assertEqual(failed.artifact_sha256, hashlib.sha256(second).hexdigest())
        for event in self.events:
            self.assertGreaterEqual(event.lock_wait_ms, 0.0)
            self.assertGreater(event.transaction_ms, 0.0)
            self.assertEqual(event.source_ref, "test.source")
        stats = coordinator.stats
        self.assertEqual(
            stats.outcome_counts,
            {OUTCOME_COMMITTED: 1, OUTCOME_FAILED_ROLLED_BACK: 1},
        )
        self.assertEqual(stats.rollbacks, 1)
        self.assertGreater(stats.transaction_ms_total, 0.0)
        self.assertGreaterEqual(stats.transaction_ms_max, committed.transaction_ms)
        self.assertGreaterEqual(stats.lock_wait_ms_total, stats.lock_wait_ms_max)

    def test_trace_callback_failure_does_not_affect_the_transaction(self) -> None:
        def broken(event):
            raise RuntimeError("trace sink failed")

        collector = RetentionCollector(
            FileSystemArtifactStore(self.root), RetentionPolicy(100, 100)
        )
        coordinator = ArtifactAdmissionCoordinator(collector, trace=broken)
        payload = payload_of("a", 10)
        coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        self.assertEqual(coordinator.stats.trace_errors, 1)
        self.assertEqual(len(self.entries(coordinator)), 1)

    def test_every_path_emits_exactly_one_event(self) -> None:
        payload = payload_of("a", 10)
        self.coordinator.admit(request_for(payload, "entry-a"), store_payload(payload))
        with self.assertRaises(RetentionIndexError):
            self.coordinator.admit(
                request_for(payload, "entry-a"), store_payload(payload)
            )
        with self.assertRaises(ArtifactAdmissionError):
            self.coordinator.admit(
                request_for(payload, "entry-b"),
                lambda port: (_ for _ in ()).throw(OSError("x")),
            )
        self.assertEqual(
            [event.outcome for event in self.events],
            [OUTCOME_COMMITTED, OUTCOME_REJECTED_INVALID, OUTCOME_FAILED_ROLLED_BACK],
        )


class RequestValidationTest(unittest.TestCase):
    def base(self, **overrides) -> dict:
        values = dict(
            payload_size=1,
            expected_sha256=None,
            artifact_type=ARTIFACT_TYPE,
            media_type=MEDIA_TYPE,
            entry_id="entry-a",
            protected=False,
            source_ref="test.source",
            sensitive=False,
        )
        values.update(overrides)
        return values

    def test_valid_request_is_accepted(self) -> None:
        ArtifactAdmissionRequest(**self.base())
        ArtifactAdmissionRequest(**self.base(expected_sha256="a" * 64, payload_size=0))

    def test_invalid_fields_are_rejected(self) -> None:
        invalid = [
            dict(payload_size=-1),
            dict(payload_size=True),
            dict(payload_size="1"),
            dict(expected_sha256="A" * 64),
            dict(expected_sha256="abc"),
            dict(artifact_type="Bad Type"),
            dict(media_type="not a mime"),
            dict(entry_id="Bad/Id"),
            dict(entry_id=""),
            dict(protected="yes"),
            dict(source_ref="../escape"),
            dict(source_ref=""),
            dict(source_ref="x" * 129),
            dict(sensitive=1),
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    ArtifactAdmissionRequest(**self.base(**overrides))

    def test_admit_rejects_wrong_argument_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            collector = RetentionCollector(
                FileSystemArtifactStore(Path(directory)), RetentionPolicy(10, 10)
            )
            coordinator = ArtifactAdmissionCoordinator(collector)
            with self.assertRaises(TypeError):
                coordinator.admit({"payload_size": 1}, lambda port: None)
            with self.assertRaises(TypeError):
                coordinator.admit(ArtifactAdmissionRequest(**self.base()), None)


if __name__ == "__main__":
    unittest.main()
