import hashlib
import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ai_game_player.artifact_admission import (
    ArtifactAdmissionCoordinator,
    ArtifactAdmissionRequest,
)
from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.event_journal import EventJournal
from ai_game_player.experience import ArtifactReference, ExperienceEpisode
from ai_game_player.experience_reader import ExperienceRecoveryError
from ai_game_player.retention_collector import (
    LocalRetentionFileSystem,
    RetentionCollector,
)
from ai_game_player.retention_index import RetentionEntry, retention_index_document
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.retention_root_lock import (
    RetentionLockTimeoutError,
    retention_root_lock,
)
from ai_game_player.runtime import retention_adapter as adapter_module
from ai_game_player.runtime.retention_adapter import (
    CompositeReferenceProvider,
    ExperienceReferenceSource,
    JournalReferenceSource,
    PinnedReferenceSource,
    RetentionReferenceError,
    RetentionReferenceSnapshot,
    RetentionTrigger,
    RetentionTriggerPolicy,
    RuntimeRetentionAdapter,
    RuntimeRetentionReport,
    SkippedRetention,
)

MEDIA_TYPE = "application/octet-stream"
SESSION = "session-one"
SRC = Path(__file__).parents[1] / "src" / "ai_game_player"


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class HookedFileSystem(LocalRetentionFileSystem):
    """object削除の直前に合図を出し、GC中の別threadの動きを決定的に作る。"""

    def __init__(self) -> None:
        self.on_remove = None

    def remove_object(self, path):
        if self.on_remove is not None:
            self.on_remove()
        super().remove_object(path)


class FakeProvider:
    """snapshotを返す・例外にする・世代を進める代役。"""

    def __init__(self, session_id: str = SESSION, clock=None) -> None:
        self.session_id = session_id
        self.clock = clock
        self.generation = 1
        self.complete = True
        self.error: Exception | None = None
        self.created_at_ms: int | None = None
        self.references: tuple[ArtifactReference, ...] = ()
        self.bump_each_call = False

    def snapshot(self) -> RetentionReferenceSnapshot:
        if self.error is not None:
            raise self.error
        generation = self.generation
        if self.bump_each_call:
            self.generation += 1
        return RetentionReferenceSnapshot(
            session_id=self.session_id,
            generation=generation,
            created_at_ms=(
                self.created_at_ms if self.created_at_ms is not None else self.clock()
            ),
            artifact_sha256s=frozenset(item.sha256 for item in self.references),
            references=self.references,
            source_positions={"fake": "n=1"},
            complete=self.complete,
            incomplete_reasons=() if self.complete else ("fake_incomplete",),
        )


def payload_of(label: str, size: int = 10) -> bytes:
    return (label.encode("ascii") * size)[:size]


def digest_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class RetentionHarness(unittest.TestCase):
    soft = 10_000
    hard = 20_000
    default_ttl_ms: int | None = 1_000
    trigger = RetentionTriggerPolicy()
    background = False
    use_fake_provider = False

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "store"
        self.clock = FakeClock()
        self.journal = EventJournal(base / "journal.sqlite3", session_id=SESSION)
        self.addCleanup(self.journal.close)
        self.file_system = HookedFileSystem()
        self.collector = RetentionCollector(
            FileSystemArtifactStore(self.root),
            RetentionPolicy(self.soft, self.hard, default_ttl_ms=self.default_ttl_ms),
            clock=self.clock,
            file_system=self.file_system,
        )
        self.admission = ArtifactAdmissionCoordinator(self.collector)
        self.pins = PinnedReferenceSource()
        self.journal_source = JournalReferenceSource(
            self.journal, batch_limit=self.trigger.journal_batch_limit
        )
        self.composite = CompositeReferenceProvider(
            SESSION, [self.journal_source], clock=self.clock, pins=self.pins
        )
        self.fake = FakeProvider(clock=self.clock)
        self.events: list = []
        self.adapter = self.make_adapter()

    def make_adapter(self, *, provider=None, trace=True, trigger=None):
        adapter = RuntimeRetentionAdapter(
            self.collector,
            self.admission,
            provider
            if provider is not None
            else (self.fake if self.use_fake_provider else self.composite),
            trigger if trigger is not None else self.trigger,
            session_id=SESSION,
            clock=self.clock,
            trace=self.events.append if trace else None,
            pins=self.pins,
            background=self.background,
        )
        self.journal.set_reference_guard(adapter.assert_recording)
        self.addCleanup(adapter.close_session)
        return adapter

    def request(self, payload: bytes, entry_id: str, *, protected=False):
        return ArtifactAdmissionRequest(
            payload_size=len(payload),
            expected_sha256=digest_of(payload),
            artifact_type="frame.raw",
            media_type=MEDIA_TYPE,
            entry_id=entry_id,
            protected=protected,
            source_ref="test.source",
        )

    def persist(self, payload: bytes, entry_id: str, *, protected=False):
        return self.adapter.persist(
            self.request(payload, entry_id, protected=protected),
            lambda port: port.put(payload),
        )

    def record(self, persisted) -> None:
        with self.adapter.recording([persisted.reference]):
            self.journal.append("test.ref", artifact_refs=(persisted.reference,))

    def digests(self) -> set[str]:
        return set(FileSystemArtifactStore(self.root)._iter_digests())

    def entry_ids(self) -> set[str]:
        return {entry.entry_id for entry in self.collector.load_entries()}

    def gc(self, trigger=RetentionTrigger.MAINTENANCE):
        return self.adapter.maybe_collect(trigger)


class SnapshotAndLiveReferenceTest(RetentionHarness):
    def test_t1_live_reference_survives_expiry_and_is_counted_as_blocked(self) -> None:
        kept = self.persist(payload_of("a"), "entry.kept")
        self.adapter.release(self.persist(payload_of("b"), "entry.gone").token)
        self.record(kept)
        self.adapter.release(self.persist(payload_of("c"), "entry.unrecorded").token)
        self.clock.now_ms += 10_000

        result = self.gc()

        self.assertIsInstance(result, RuntimeRetentionReport)
        self.assertIn(kept.reference.sha256, self.digests())
        self.assertIn("entry.kept", self.entry_ids())
        self.assertEqual(1, result.blocked_refs)
        self.assertNotIn("entry.unrecorded", self.entry_ids())

    def test_t2_reference_removed_from_pins_is_deleted_next_run(self) -> None:
        pinned = self.persist(payload_of("a"), "entry.pinned")
        self.clock.now_ms += 10_000
        self.gc()
        self.assertIn(pinned.reference.sha256, self.digests())

        self.adapter.release(pinned.token)
        self.clock.now_ms += 10_000
        self.gc()

        self.assertNotIn(pinned.reference.sha256, self.digests())

    def test_t4_close_session_keeps_valid_references_and_removes_the_rest(self) -> None:
        recorded = self.persist(payload_of("a"), "entry.recorded")
        self.record(recorded)
        pinned = self.persist(payload_of("b"), "entry.pinned")
        released = self.persist(payload_of("c"), "entry.released")
        self.adapter.release(released.token)
        self.clock.now_ms += 10_000

        result = self.adapter.close_session()

        self.assertIsInstance(result, RuntimeRetentionReport)
        self.assertEqual(
            {recorded.reference.sha256, pinned.reference.sha256}, self.digests()
        )
        self.assertIs(result, self.adapter.last_close_result)
        self.assertIs(result, self.adapter.close_session())
        with self.assertRaises(RuntimeError):
            self.adapter.reserve("0" * 64, reason="after.close")

    def test_t10_journal_source_reads_only_the_increment(self) -> None:
        first = self.persist(payload_of("a"), "entry.first")
        self.record(first)
        calls: list[int] = []
        original = self.journal.artifact_refs_after

        def spy(sequence, *, limit):
            calls.append(sequence)
            return original(sequence, limit=limit)

        with mock.patch.object(self.journal, "artifact_refs_after", spy):
            self.composite.snapshot()
            self.composite.snapshot()
            second = self.persist(payload_of("b"), "entry.second")
            self.record(second)
            snapshot = self.composite.snapshot()

        self.assertEqual([0, 1, 1], calls)
        self.assertEqual(
            {first.reference.sha256, second.reference.sha256},
            set(snapshot.artifact_sha256s),
        )
        self.assertEqual("seq=2", snapshot.source_positions["journal"])
        self.assertEqual(1, self.journal_source.reference_count(first.reference.sha256))

    def test_journal_catchup_pending_is_incomplete_then_completes(self) -> None:
        for number in range(3):
            self.journal.append(f"test.plain.{number}")
        source = JournalReferenceSource(self.journal, batch_limit=2)
        provider = CompositeReferenceProvider(SESSION, [source], clock=self.clock)

        first = provider.snapshot()
        second = provider.snapshot()

        self.assertFalse(first.complete)
        self.assertEqual(("journal_catchup_pending",), first.incomplete_reasons)
        self.assertTrue(second.complete)

    def test_snapshot_rejects_inconsistent_digest_sets(self) -> None:
        reference = ArtifactReference("a.one", "a" * 64, MEDIA_TYPE, False)
        with self.assertRaises(ValueError):
            RetentionReferenceSnapshot(
                SESSION, 0, 0, frozenset({"b" * 64}), (reference,), {}, True
            )
        with self.assertRaises(ValueError):
            RetentionReferenceSnapshot(SESSION, 0, 0, frozenset(), (), {}, False)


class FailClosedTest(RetentionHarness):
    use_fake_provider = True

    def expired_store(self) -> tuple[set[str], set[str]]:
        for number in range(3):
            self.adapter.release(
                self.persist(payload_of(f"p{number}"), f"entry.{number}").token
            )
        self.clock.now_ms += 10_000
        self.fake.created_at_ms = None
        return self.digests(), self.entry_ids()

    def assert_untouched(self, before, result, reason) -> None:
        self.assertIsInstance(result, SkippedRetention)
        self.assertEqual(reason, result.reason)
        self.assertEqual(before, (self.digests(), self.entry_ids()))

    def test_t3_provider_error_incomplete_stale_and_mismatch_delete_nothing(
        self,
    ) -> None:
        before = self.expired_store()

        self.fake.error = RuntimeError("boom")
        self.assert_untouched(before, self.gc(), "snapshot_failed")
        self.fake.error = None

        self.fake.complete = False
        self.assert_untouched(before, self.gc(), "snapshot_incomplete")
        self.fake.complete = True

        self.fake.created_at_ms = self.clock.now_ms - 5_000
        self.assert_untouched(before, self.gc(), "snapshot_stale")
        self.fake.created_at_ms = None

        self.fake.generation = 5
        self.fake.bump_each_call = True
        self.assert_untouched(before, self.gc(), "generation_mismatch")
        self.fake.bump_each_call = False
        self.fake.generation = 5
        self.assertIsInstance(self.gc(), RuntimeRetentionReport)

    def test_t3_generation_regression_and_other_session_delete_nothing(self) -> None:
        before = self.expired_store()
        self.fake.generation = 7
        self.fake.session_id = "another-session"
        self.assert_untouched(before, self.gc(), "session_mismatch")
        self.fake.session_id = SESSION
        # 世代7を受理した後に3へ戻った
        self.fake.generation = 7
        self.clock.now_ms += 10_000
        self.assertIsInstance(self.gc(), RuntimeRetentionReport)
        for number in range(2):
            self.adapter.release(
                self.persist(payload_of(f"r{number}"), f"entry.r{number}").token
            )
        self.clock.now_ms += 10_000
        before_regression = (self.digests(), self.entry_ids())
        self.fake.generation = 3
        result = self.gc()
        self.assert_untouched(before_regression, result, "snapshot_stale")

    def test_t3_corrupt_journal_row_is_snapshot_failed_with_nothing_deleted(
        self,
    ) -> None:
        self.use_fake_provider = False
        adapter = self.make_adapter(provider=self.composite)
        persisted = self.persist(payload_of("a"), "entry.one")
        with adapter.recording([persisted.reference]):
            self.journal.append("test.ref", artifact_refs=(persisted.reference,))
        adapter.release(persisted.token)
        self.clock.now_ms += 10_000
        before = (self.digests(), self.entry_ids())
        connection = sqlite3.connect(self.journal.path)
        connection.execute("UPDATE events SET envelope_json='{broken' WHERE sequence=1")
        connection.commit()
        connection.close()

        result = adapter.maybe_collect(RetentionTrigger.MAINTENANCE)

        self.assert_untouched(before, result, "snapshot_failed")

    def test_lock_timeout_and_absent_store_delete_nothing(self) -> None:
        absent = self.adapter.maybe_collect(RetentionTrigger.MAINTENANCE)
        self.assertEqual("store_absent", absent.reason)
        self.assertFalse(self.root.exists())

        before = self.expired_store()
        release = threading.Event()
        acquired = threading.Event()

        def hold() -> None:
            with retention_root_lock(self.root, timeout_seconds=5):
                acquired.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(acquired.wait(5))
        try:
            fast = RetentionTriggerPolicy(trigger_lock_timeout_seconds=0.05)
            result = self.make_adapter(trigger=fast).maybe_collect(
                RetentionTrigger.MAINTENANCE
            )
        finally:
            release.set()
            thread.join()
        self.assert_untouched(before, result, "lock_timeout")

    def test_corrupt_index_is_index_error_and_changes_nothing(self) -> None:
        self.expired_store()
        index = self.collector.index_path
        index.write_text("{broken", encoding="utf-8")
        before = self.digests()

        result = self.gc()

        self.assertEqual("index_error", result.reason)
        self.assertEqual(before, self.digests())


class PressureAndTraceTest(RetentionHarness):
    soft = 15
    hard = 25
    default_ttl_ms = None

    def test_t6_protected_store_over_budget_reports_pressure_without_deleting(
        self,
    ) -> None:
        self.persist(payload_of("a"), "entry.a", protected=True)
        self.persist(payload_of("b"), "entry.b", protected=True)
        self.clock.now_ms += 10_000

        result = self.gc()

        self.assertIsInstance(result, RuntimeRetentionReport)
        self.assertEqual(0, len(result.report.deleted_artifacts))
        self.assertEqual("over_budget", result.pressure)
        self.assertEqual("over_budget", self.adapter.pressure)
        self.assertEqual("pressure", self.events[-1].outcome)

        FileSystemArtifactStore(self.root).put(
            payload_of("z", 30), media_type=MEDIA_TYPE, artifact_type="frame.raw"
        )
        self.clock.now_ms += 10_000
        result = self.gc()
        self.assertEqual("over_hard_limit", result.pressure)
        self.assertEqual("over_hard_limit", self.adapter.pressure)

    def test_t12_trace_has_metrics_and_no_digest_or_path(self) -> None:
        persisted = self.persist(payload_of("a"), "entry.a")
        self.adapter.release(persisted.token)
        self.clock.now_ms += 10_000
        self.gc()

        payload = self.events[-1].to_payload()
        text = json.dumps(payload)

        for key in (
            "gc_count",
            "removed_entries",
            "deleted_artifacts",
            "removed_bytes",
            "bytes_after",
            "blocked_refs",
            "deferred_artifacts",
            "over_budget",
            "over_hard_limit",
            "snapshot_generation",
            "source_positions",
            "lock_wait_ms",
            "snapshot_ms",
            "collect_ms",
            "total_ms",
            "gate_wait_ms",
        ):
            self.assertIn(key, payload)
        self.assertEqual("retention.gc/v1", adapter_module.TRACE_EVENT_TYPE)
        self.assertNotIn(persisted.reference.sha256, text)
        self.assertNotIn(str(self.root), text)
        self.assertEqual(1, payload["gc_count"])

    def test_t12_trace_failure_does_not_change_the_gc_result(self) -> None:
        def broken(_event) -> None:
            raise RuntimeError("trace failed")

        adapter = RuntimeRetentionAdapter(
            self.collector,
            self.admission,
            self.composite,
            self.trigger,
            session_id=SESSION,
            clock=self.clock,
            trace=broken,
            pins=self.pins,
            background=False,
        )
        self.addCleanup(adapter.close_session)
        self.adapter.release(self.persist(payload_of("a"), "entry.a").token)
        self.clock.now_ms += 10_000

        result = adapter.maybe_collect(RetentionTrigger.MAINTENANCE)

        self.assertIsInstance(result, RuntimeRetentionReport)
        self.assertEqual(1, adapter.stats.trace_errors)
        self.assertEqual(1, adapter.stats.gc_count)


class TriggerAndLimitTest(RetentionHarness):
    default_ttl_ms = 1_000
    trigger = RetentionTriggerPolicy(
        persist_count_threshold=3, max_deleted_artifacts_per_run=25
    )

    def test_t7_triggers_coalesce_inside_the_minimum_interval(self) -> None:
        for number in range(3):
            self.adapter.release(
                self.persist(payload_of(f"p{number}"), f"entry.{number}").token
            )
        self.clock.now_ms += 10_000

        results = [
            self.adapter.maybe_collect(RetentionTrigger.PERSIST_COUNT)
            for _ in range(1000)
        ]

        reports = [item for item in results if isinstance(item, RuntimeRetentionReport)]
        self.assertEqual(1, len(reports))
        self.assertEqual(
            999,
            sum(1 for item in results if getattr(item, "reason", "") == "coalesced"),
        )
        self.assertEqual(1, self.adapter.stats.gc_count)

    def test_t7_conditions_not_met_are_not_due(self) -> None:
        self.persist(payload_of("a"), "entry.a")
        results = [
            self.adapter.maybe_collect(RetentionTrigger.PERSIST_COUNT)
            for _ in range(1000)
        ]
        self.assertTrue(
            all(getattr(item, "reason", "") == "not_due" for item in results)
        )
        self.assertEqual(0, self.adapter.stats.gc_count)

    def test_t7_request_does_not_block_the_caller(self) -> None:
        adapter = self.make_adapter(provider=self.composite)
        threaded = RuntimeRetentionAdapter(
            self.collector,
            self.admission,
            self.composite,
            self.trigger,
            session_id=SESSION,
            clock=self.clock,
            pins=self.pins,
            background=True,
        )
        self.addCleanup(threaded.close_session)
        started = time.perf_counter()
        for _ in range(1000):
            threaded.request(RetentionTrigger.PERSIST_COUNT)
        elapsed = time.perf_counter() - started
        threaded.close_session()
        self.assertLess(elapsed, 2.0)
        self.assertEqual(1000, threaded.stats.requests)
        self.assertIsNotNone(adapter)

    def test_t9_work_limit_defers_the_rest_without_counting_it_as_blocked(self) -> None:
        # Collectorの計画は索引全件のmetadataを読むため、件数を縮小して同じ分岐を試す
        total = 30
        # admissionは1件ごとに全件を計測するため、300件はStoreへ直接putして索引を一度に書く
        store = FileSystemArtifactStore(self.root)
        entries = []
        for number in range(total):
            payload = payload_of(f"{number:04d}", 12)
            reference = store.put(
                payload, media_type=MEDIA_TYPE, artifact_type="frame.raw"
            )
            entries.append(
                RetentionEntry(
                    f"entry.{number}",
                    reference.sha256,
                    len(payload),
                    "frame.raw",
                    0,
                    500,
                    False,
                )
            )
        self.file_system.write_index(
            self.collector.index_path, retention_index_document(entries)
        )
        self.clock.now_ms += 10_000

        first = self.gc(RetentionTrigger.MAINTENANCE)

        self.assertIsInstance(first, RuntimeRetentionReport)
        self.assertEqual(25, len(first.report.deleted_artifacts))
        self.assertEqual(5, first.deferred_artifacts)
        self.assertEqual(0, first.blocked_refs)
        self.assertEqual(5, len(self.digests()))
        self.assertEqual(5, len(self.entry_ids()))

        self.clock.now_ms += 10_000
        second = self.gc(RetentionTrigger.PERIODIC)

        self.assertEqual(5, len(second.report.deleted_artifacts))
        self.assertEqual(0, second.deferred_artifacts)
        self.assertEqual(set(), self.digests())

    def test_t14_close_ignores_the_minimum_interval(self) -> None:
        self.adapter.release(self.persist(payload_of("a"), "entry.a").token)
        self.clock.now_ms += 10_000
        first = self.gc()
        self.assertIsInstance(first, RuntimeRetentionReport)
        self.adapter.release(self.persist(payload_of("b"), "entry.b").token)
        self.clock.now_ms += 2_000

        final = self.adapter.close_session()

        self.assertIsInstance(final, RuntimeRetentionReport)
        self.assertEqual(set(), self.digests())

    def test_policy_rejects_contradictory_values(self) -> None:
        for bad in (
            {"min_interval_ms": -1},
            {"persist_count_threshold": 0},
            {"trigger_lock_timeout_seconds": 0},
            {"close_lock_timeout_seconds": float("inf")},
            {"journal_batch_limit": 10**9},
            {"max_deleted_artifacts_per_run": True},
        ):
            with self.assertRaises(ValueError, msg=str(bad)):
                RetentionTriggerPolicy(**bad)


class ProductionDeletionTest(RetentionHarness):
    def test_t11_only_the_adapter_calls_collect_in_production_code(self) -> None:
        callers = sorted(
            path.name
            for path in SRC.rglob("*.py")
            if re.search(r"\.collect\(", path.read_text(encoding="utf-8"))
        )
        self.assertEqual(["retention_adapter.py"], callers)

    def test_t11_collect_always_receives_the_verified_live_references(self) -> None:
        recorded = self.persist(payload_of("a"), "entry.a")
        self.record(recorded)
        self.adapter.release(self.persist(payload_of("b"), "entry.b").token)
        self.clock.now_ms += 10_000
        original = self.collector.collect
        calls: list[tuple] = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        with mock.patch.object(self.collector, "collect", spy):
            self.gc()

        self.assertEqual(1, len(calls))
        args, kwargs = calls[0]
        self.assertEqual({}, kwargs)
        self.assertEqual(1, len(args))
        self.assertIn(recorded.reference.sha256, {item.sha256 for item in args[0]})


class RecordingGateTest(RetentionHarness):
    def test_t13_missing_object_is_rejected_and_nothing_is_written(self) -> None:
        ghost = ArtifactReference("ghost.one", "c" * 64, MEDIA_TYPE, False)
        before = self.journal.last_sequence

        with self.assertRaises(RetentionReferenceError):
            with self.adapter.recording([ghost]):
                self.journal.append("test.ref", artifact_refs=(ghost,))

        self.assertEqual(before, self.journal.last_sequence)

    def test_t13_append_outside_recording_is_rejected_by_the_guard(self) -> None:
        persisted = self.persist(payload_of("a"), "entry.a")
        before = self.journal.last_sequence

        with self.assertRaises(RetentionReferenceError):
            self.journal.append("test.ref", artifact_refs=(persisted.reference,))
        other = ArtifactReference("other.one", "d" * 64, MEDIA_TYPE, False)
        with self.adapter.recording([persisted.reference]):
            with self.assertRaises(RetentionReferenceError):
                self.journal.append("test.ref", artifact_refs=(other,))
        self.journal.append("test.plain")

        self.assertEqual(before + 1, self.journal.last_sequence)

    def test_t13_exception_inside_recording_keeps_the_pin(self) -> None:
        persisted = self.persist(payload_of("a"), "entry.a")

        with self.assertRaises(RuntimeError):
            with self.adapter.recording([persisted.reference]):
                raise RuntimeError("append outcome unknown")

        self.assertIn(persisted.reference.sha256, self.pins.digests())
        self.record(persisted)
        self.assertNotIn(persisted.reference.sha256, self.pins.digests())

    def test_t13_gate_timeout_raises_and_writes_nothing(self) -> None:
        persisted = self.persist(payload_of("a"), "entry.a")
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with self.adapter._gate:
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(5))
        before = self.journal.last_sequence
        try:
            with self.assertRaises(RetentionLockTimeoutError):
                with self.adapter.recording(
                    [persisted.reference], timeout_seconds=0.05
                ):
                    self.journal.append(
                        "test.ref", artifact_refs=(persisted.reference,)
                    )
        finally:
            release.set()
            thread.join()
        self.assertEqual(before, self.journal.last_sequence)
        self.assertIn(persisted.reference.sha256, self.pins.digests())

    def test_invalid_recording_target_is_rejected(self) -> None:
        for target in ("elsewhere", "experience:", "experience:../x"):
            with self.assertRaises(RetentionReferenceError, msg=target):
                with self.adapter.recording([], target=target):
                    pass

    def test_guard_rejects_values_that_are_not_artifact_references(self) -> None:
        with self.assertRaises(RetentionReferenceError):
            self.adapter.assert_recording(("not-a-reference",))

    def test_persist_requires_digest_and_matching_token(self) -> None:
        payload = payload_of("a")
        request = ArtifactAdmissionRequest(
            payload_size=len(payload),
            expected_sha256=None,
            artifact_type="frame.raw",
            media_type=MEDIA_TYPE,
            entry_id="entry.nodigest",
            protected=False,
            source_ref="test.source",
        )
        with self.assertRaises(ValueError):
            self.adapter.persist(request, lambda port: port.put(payload))
        persisted = self.persist(payload, "entry.a")
        other = self.adapter.reserve("e" * 64, reason="other")
        with self.assertRaises(ValueError):
            self.adapter.on_artifact_persisted(other, persisted.reference, "entry.a")

    def test_failed_admission_releases_its_pin(self) -> None:
        payload = payload_of("a")

        def failing_writer(_port):
            raise OSError("writer failed")

        with self.assertRaises(Exception):
            self.adapter.persist(self.request(payload, "entry.a"), failing_writer)

        self.assertEqual(frozenset(), self.pins.digests())

    def test_protected_flag_marks_the_entry_protected(self) -> None:
        persisted = self.persist(payload_of("a"), "entry.a")
        self.adapter.on_artifact_persisted(
            persisted.token, persisted.reference, "entry.a", protected=True
        )
        entry = next(
            item for item in self.collector.load_entries() if item.entry_id == "entry.a"
        )
        self.assertTrue(entry.protected)


class ConcurrencyTest(RetentionHarness):
    default_ttl_ms = 1

    def test_t5_concurrent_persist_record_and_gc_leave_no_dangling_reference(
        self,
    ) -> None:
        self.clock.now_ms = int(time.time() * 1000)
        errors: list[BaseException] = []
        done = threading.Event()

        def worker(index: int) -> None:
            try:
                for number in range(6):
                    persisted = self.persist(
                        payload_of(f"w{index}n{number}", 16), f"entry.{index}.{number}"
                    )
                    while True:
                        try:
                            self.record(persisted)
                            break
                        except RetentionLockTimeoutError:
                            continue
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        def collector_loop() -> None:
            while not done.is_set():
                self.clock.now_ms = int(time.time() * 1000)
                self.adapter.maybe_collect(RetentionTrigger.MAINTENANCE)
                time.sleep(0.01)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        gc_thread = threading.Thread(target=collector_loop)
        gc_thread.start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        done.set()
        gc_thread.join(60)

        self.assertEqual([], errors)
        batch = self.journal.artifact_refs_after(0, limit=100_000)
        self.assertEqual(24, len(batch.refs))
        present = self.digests()
        self.assertEqual(
            [], [item.sha256 for item in batch.refs if item.sha256 not in present]
        )
        self.assertGreater(self.adapter.stats.gc_count, 0)

    def test_t5b_persist_during_gc_always_returns_an_existing_object(self) -> None:
        for same_digest_is_referenced in (False, True):
            with self.subTest(referenced=same_digest_is_referenced):
                self.tearDown_store()
                shared = payload_of("shared", 16)
                first = self.persist(shared, "entry.shared.1")
                if same_digest_is_referenced:
                    self.record(first)
                self.adapter.release(first.token)
                self.adapter.release(
                    self.persist(payload_of("victim", 16), "entry.victim").token
                )
                self.clock.now_ms += 10_000
                outcome: dict = {}
                started = threading.Event()

                def second_persist() -> None:
                    started.set()
                    outcome["persisted"] = self.persist(shared, "entry.shared.2")

                thread = threading.Thread(target=second_persist)

                def during_removal() -> None:
                    self.file_system.on_remove = None
                    thread.start()
                    started.wait(5)
                    time.sleep(0.3)
                    self.assertTrue(thread.is_alive())

                self.file_system.on_remove = during_removal
                self.assertIsInstance(self.gc(), RuntimeRetentionReport)
                thread.join(10)

                persisted = outcome["persisted"]
                self.assertTrue(
                    Path(
                        self.collector.store.object_path(persisted.reference.sha256)
                    ).is_dir()
                )
                self.assertIn("entry.shared.2", self.entry_ids())

    def tearDown_store(self) -> None:
        # subTestごとに空のStoreから始める
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)
        self.pins.clear()


def experience_line(*references: ArtifactReference) -> str:
    episode = ExperienceEpisode(
        "episode.one", "2026-09-30T10:00:00Z", "runtime", (), artifacts=references
    )
    return json.dumps(episode.to_dict(), sort_keys=True) + "\n"


class ExperienceSourceTest(RetentionHarness):
    def setUp(self) -> None:
        super().setUp()
        self.experience_path = Path(self.temp.name) / "experience.jsonl"
        self.experience = ExperienceReferenceSource(
            "experience.jsonl", self.experience_path
        )
        self.provider = CompositeReferenceProvider(
            SESSION,
            [self.journal_source, self.experience],
            clock=self.clock,
            pins=self.pins,
        )
        self.adapter = self.make_adapter(provider=self.provider)

    def test_experience_reference_protects_until_it_is_removed(self) -> None:
        kept = self.persist(payload_of("a"), "entry.kept")
        with self.adapter.recording(
            [kept.reference], target="experience:experience.jsonl"
        ):
            self.experience_path.write_text(
                experience_line(kept.reference), encoding="utf-8"
            )
        self.clock.now_ms += 10_000
        self.assertIsInstance(self.gc(), RuntimeRetentionReport)
        self.assertIn(kept.reference.sha256, self.digests())

        with self.adapter.recording([], target="experience:experience.jsonl"):
            self.experience_path.write_text(experience_line(), encoding="utf-8")
        self.clock.now_ms += 10_000
        self.gc()
        self.assertNotIn(kept.reference.sha256, self.digests())

    def test_t15_recording_forces_a_reread_even_when_stat_is_identical(self) -> None:
        reference = ArtifactReference("one.ref", "a" * 64, MEDIA_TYPE, False)
        other = ArtifactReference("two.ref", "b" * 64, MEDIA_TYPE, False)
        first_text = experience_line(reference)
        second_text = experience_line(other)
        self.assertEqual(len(first_text), len(second_text))
        self.experience_path.write_text(first_text, encoding="utf-8")
        stat = self.experience_path.stat()
        before = self.experience.read()
        self.experience_path.write_text(second_text, encoding="utf-8")
        import os

        os.utime(self.experience_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        cached = self.experience.read()
        self.experience.mark_changed()
        fresh = self.experience.read()

        self.assertEqual({"a" * 64}, {item.sha256 for item in before.references})
        self.assertEqual(before.references, cached.references)
        self.assertEqual({"b" * 64}, {item.sha256 for item in fresh.references})
        self.assertGreater(fresh.changes, before.changes)

    def test_t15_stat_change_during_read_is_incomplete(self) -> None:
        self.experience_path.write_text(experience_line(), encoding="utf-8")
        real = adapter_module._stat_identity
        calls = []

        def shifting(path):
            calls.append(path)
            value = real(path)
            return value if len(calls) == 1 else (*value[:3], value[3] + 1)

        with mock.patch.object(adapter_module, "_stat_identity", shifting):
            reading = self.experience.read()

        self.assertFalse(reading.complete)
        self.assertIn("experience_changed_during_read", reading.reasons)

    def test_corrupt_tail_is_incomplete_and_interior_corruption_fails(self) -> None:
        good = experience_line()
        self.experience_path.write_text(good + "{broken", encoding="utf-8")
        reading = self.experience.read()
        self.assertFalse(reading.complete)
        self.assertEqual(("experience_recovered_tail",), reading.reasons)

        self.experience.mark_changed()
        self.experience_path.write_text("{broken\n" + good, encoding="utf-8")
        with self.assertRaises(ExperienceRecoveryError):
            self.experience.read()

    def test_absent_file_is_empty_until_it_disappears_after_existing(self) -> None:
        self.assertTrue(self.experience.read().complete)
        self.experience_path.write_text(experience_line(), encoding="utf-8")
        self.assertTrue(self.experience.read().complete)
        self.experience_path.unlink()
        reading = self.experience.read()
        self.assertFalse(reading.complete)
        self.assertEqual(("experience_missing",), reading.reasons)

    def test_oversized_file_is_rejected(self) -> None:
        small = ExperienceReferenceSource(
            "small.jsonl", self.experience_path, max_bytes=8
        )
        self.experience_path.write_text(experience_line(), encoding="utf-8")
        with self.assertRaises(ValueError):
            small.read()

    def test_t15_reference_free_journal_event_does_not_change_the_snapshot(
        self,
    ) -> None:
        first = self.provider.snapshot()
        self.journal.append("test.plain")
        second = self.provider.snapshot()
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(first.references, second.references)
        self.assertEqual("seq=1", second.source_positions["journal"])


class PinSourceTest(unittest.TestCase):
    def test_pin_release_and_version(self) -> None:
        pins = PinnedReferenceSource()
        first = pins.pin("a" * 64, "first")
        second = pins.pin("a" * 64, "second")
        self.assertEqual(frozenset({"a" * 64}), pins.digests())
        self.assertTrue(pins.release_digest("a" * 64))
        self.assertTrue(pins.release(second))
        self.assertFalse(pins.release(first))
        self.assertEqual(frozenset(), pins.digests())
        self.assertGreaterEqual(pins.version, 4)
        with self.assertRaises(ValueError):
            pins.pin("not-a-digest", "bad")
        with self.assertRaises(ValueError):
            pins.pin("a" * 64, "")


if __name__ == "__main__":
    unittest.main()
