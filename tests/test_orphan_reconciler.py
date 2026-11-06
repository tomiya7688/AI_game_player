import datetime
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from ai_game_player.admission_pending_store import (
    PendingAdmission,
    PendingAdmissionStore,
)
from ai_game_player.artifact_admission import (
    ArtifactAdmissionCoordinator,
    ArtifactAdmissionRequest,
)
from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.artifact_trash import (
    KIND_OBJECT,
    REASON_RETENTION_REMOVE,
    TRASH_DIRECTORY_NAME,
    ArtifactTrash,
)
from ai_game_player.experience import ArtifactReference
from ai_game_player.orphan_reconciler import (
    STATE_ACTIVE_PENDING,
    STATE_CORRUPT_OR_UNKNOWN,
    STATE_LIVE_REFERENCED,
    STATE_RECENT_UNINDEXED,
    STATE_SAFE_ORPHAN,
    TRIGGER_EXPLICIT,
    TRIGGER_PERIODIC,
    LiveReferenceSnapshot,
    OrphanReconciler,
    OrphanReconcilerConfig,
    reclaim_budget,
)
from ai_game_player.retention_collector import (
    LocalRetentionFileSystem,
    RetentionCollector,
)
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.retention_root_lock import RetentionRootUnsafeError
from tests.test_retention_root_lock import make_directory_link

MEDIA_TYPE = "application/octet-stream"
ARTIFACT_TYPE = "frame.raw"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


class FakeClock:
    def __init__(self) -> None:
        self.now_ms = int(time.time() * 1000)

    def __call__(self) -> int:
        return self.now_ms


class StubSource:
    def __init__(self, references=(), *, complete=True, error=None, delay=0.0):
        self.references = tuple(references)
        self.complete = complete
        self.error = error
        self.delay = delay

    def snapshot(self) -> LiveReferenceSnapshot:
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return LiveReferenceSnapshot("stub", self.references, self.complete)


def tree_listing(root: Path) -> dict[str, bytes | None]:
    """root配下の全file/dirとbytesを比較用に列挙する（lock fileは協調用なので除く）。"""
    listing: dict[str, bytes | None] = {}
    for current, directories, files in os.walk(root):
        for name in directories:
            listing[str(Path(current, name).relative_to(root))] = None
        for name in files:
            if name == ".retention.lock":
                continue
            path = Path(current, name)
            listing[str(path.relative_to(root))] = path.read_bytes()
    return listing


class ReconcilerCase(unittest.TestCase):
    soft = 10_000
    hard = 100_000

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        self.clock = FakeClock()
        self.store = FileSystemArtifactStore(self.root)
        self.collector = self.make_collector()
        self.admission = ArtifactAdmissionCoordinator(self.collector)
        self.source = StubSource()
        self.reconciler = self.make_reconciler()

    def make_collector(self, policy=None, file_system=None, timeout=5.0):
        return RetentionCollector(
            self.store,
            policy or RetentionPolicy(self.soft, self.hard),
            clock=self.clock,
            file_system=file_system,
            lock_timeout_seconds=timeout,
        )

    def make_reconciler(self, **kwargs) -> OrphanReconciler:
        kwargs.setdefault("config", OrphanReconcilerConfig())
        sources = kwargs.pop("live_sources", [self.source])
        collector = kwargs.pop("collector", self.collector)
        admission = kwargs.pop("admission", self.admission)
        return OrphanReconciler(collector, admission, sources, **kwargs)

    # --- 準備用 ---
    def put(self, label: str, size: int = 20) -> str:
        payload = (label.encode("ascii") * size)[:size]
        return self.store.put(
            payload, media_type=MEDIA_TYPE, artifact_type=ARTIFACT_TYPE
        ).sha256

    def reference(self, digest: str) -> ArtifactReference:
        return ArtifactReference(f"sha256.{digest}", digest, MEDIA_TYPE, False)

    def set_activity(self, digest: str, age_ms: int) -> None:
        """metadataのcreated_atと全mtimeを、clockから見てage_ms前に設定する。"""
        when_ms = self.clock.now_ms - age_ms
        path = self.store.object_path(digest)
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        metadata["created_at"] = datetime.datetime.fromtimestamp(
            when_ms / 1000, datetime.timezone.utc
        ).isoformat()
        (path / "metadata.json").write_text(
            json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
        )
        for target in (path / "metadata.json", path / "payload", path):
            os.utime(target, ns=(when_ms * 1_000_000, when_ms * 1_000_000))

    def orphan(self, label: str, age_ms: int = 2 * DAY_MS, size: int = 20) -> str:
        digest = self.put(label, size)
        self.set_activity(digest, age_ms)
        return digest

    def register(self, label: str) -> str:
        digest = self.put(label)
        self.collector.register(self.reference(digest), entry_id=f"entry-{label}")
        return digest

    def exists(self, digest: str) -> bool:
        return self.store.object_path(digest).is_dir()

    def candidates(self, plan) -> dict[str, tuple[str, str]]:
        return {item.location: (item.state, item.reason) for item in plan.candidates}

    def pending_record(self, digest: str, *, created_object: bool = True) -> str:
        admission_id = uuid.uuid4().hex
        PendingAdmissionStore(self.root).write(
            PendingAdmission(
                admission_id=admission_id,
                artifact_sha256=digest,
                created_object=created_object,
                entry_id=f"e-{admission_id[:8]}",
                source_ref="test.source",
                artifact_type=ARTIFACT_TYPE,
                media_type=MEDIA_TYPE,
                payload_size=20,
                protected=False,
                started_at_ms=0,
                owner_pid=1,
            )
        )
        return admission_id

    def location(self, digest: str) -> str:
        return f"{digest[:2]}/{digest}"

    def staging(self, shard: str, *, files=("payload", "metadata.json"), age_ms=None):
        directory = self.root / shard / f".pending-{uuid.uuid4().hex[:8]}"
        directory.mkdir(parents=True)
        for name in files:
            (directory / name).write_bytes(b"partial")
        if age_ms is not None:
            when = (self.clock.now_ms - age_ms) / 1000
            for target in (*directory.iterdir(), directory):
                os.utime(target, (when, when))
        return directory


class ClassificationTest(ReconcilerCase):
    def test_t1_only_safe_orphans_are_deleted_and_each_state_is_reported(self):
        indexed = self.register("a")
        safe = self.orphan("b")
        recent = self.orphan("c", age_ms=HOUR_MS)
        pending = self.orphan("d")
        self.pending_record(pending)
        live = self.orphan("e")
        self.source.references = (self.reference(live),)
        corrupt = self.orphan("f")
        (self.store.object_path(corrupt) / "metadata.json").write_text("{bad")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        states = self.candidates(plan)
        self.assertNotIn(self.location(indexed), states)
        self.assertEqual(states[self.location(safe)][0], STATE_SAFE_ORPHAN)
        self.assertEqual(states[self.location(recent)][0], STATE_RECENT_UNINDEXED)
        self.assertEqual(states[self.location(pending)][0], STATE_ACTIVE_PENDING)
        self.assertEqual(states[self.location(live)][0], STATE_LIVE_REFERENCED)
        self.assertEqual(states[self.location(corrupt)][0], STATE_CORRUPT_OR_UNKNOWN)
        self.assertEqual([t.sha256 for t in plan.targets], [safe])
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(report.deleted, (self.location(safe),))
        self.assertEqual(report.deleted_bytes, plan.targets[0].size_bytes)
        for kept in (indexed, recent, pending, live, corrupt):
            self.assertTrue(self.exists(kept), kept)
        self.assertFalse(self.exists(safe))

    def test_t2_pending_record_protects_even_old_objects(self):
        digest = self.orphan("a")
        self.pending_record(digest)
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(report.deleted, ())
        self.assertTrue(self.exists(digest))

    def test_t3_live_sources_and_extra_live_generator_protect(self):
        a = self.orphan("a")
        b = self.orphan("b")
        c = self.orphan("c")
        self.source.references = (self.reference(a),)
        report = self.reconciler.reconcile(
            TRIGGER_EXPLICIT, extra_live=(self.reference(b) for _ in range(1))
        )
        self.assertEqual(report.deleted, (self.location(c),))
        self.assertTrue(self.exists(a) and self.exists(b))

    def test_t3_incomplete_failing_or_slow_live_sources_delete_nothing(self):
        digest = self.orphan("a")
        cases = [
            StubSource(complete=False),
            StubSource(error=RuntimeError("boom")),
            StubSource(delay=0.05),
        ]
        config = OrphanReconcilerConfig(live_snapshot_timeout_ms=1)
        for source in cases:
            reconciler = self.make_reconciler(live_sources=[source], config=config)
            plan = reconciler.plan(TRIGGER_EXPLICIT)
            self.assertIsNotNone(plan.blocked_reason)
            self.assertEqual(plan.targets, ())
            report = reconciler.reconcile(TRIGGER_EXPLICIT)
            self.assertEqual(report.deleted, ())
            self.assertTrue(self.exists(digest))

    def test_t4_grace_future_time_and_clock_rollback(self):
        recent = self.orphan("a", age_ms=HOUR_MS)
        future = self.orphan("b", age_ms=-DAY_MS)
        old = self.orphan("c")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        self.assertEqual([t.sha256 for t in plan.targets], [old])
        states = self.candidates(plan)
        self.assertEqual(states[self.location(future)][0], STATE_RECENT_UNINDEXED)
        self.assertEqual(states[self.location(recent)][0], STATE_RECENT_UNINDEXED)
        # plan後に時計が戻ると、削除0（巻き戻し）。
        self.clock.now_ms -= 3 * DAY_MS
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(report.blocked_reason, "clock_rollback")
        self.assertEqual(report.deleted, ())
        self.assertTrue(self.exists(old))

    def test_t4_small_clock_rollback_is_caught_by_per_target_grace_check(self):
        old = self.orphan("a", age_ms=DAY_MS + 1000)
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        self.assertEqual(len(plan.targets), 1)
        self.clock.now_ms -= 10_000
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(report.skipped, ((self.location(old), "within_grace"),))
        self.assertTrue(self.exists(old))


class CrashLeftoverTest(ReconcilerCase):
    def test_t5_created_object_after_put_before_register_is_recovered(self):
        digest = self.orphan("a")
        self.pending_record(digest, created_object=True)
        report = self.reconciler.reconcile(TRIGGER_EXPLICIT)
        self.assertEqual(report.recovered_pending.deleted_objects, (digest,))
        self.assertFalse(self.exists(digest))
        self.assertEqual(self.collector.load_entries(), ())

    def test_t5_preexisting_object_is_collected_after_grace_once_record_is_gone(self):
        digest = self.orphan("a")
        self.pending_record(digest, created_object=False)
        first = self.reconciler.reconcile(TRIGGER_EXPLICIT)
        self.assertEqual(first.recovered_pending.removed_records[0] != "", True)
        self.assertTrue(self.exists(digest))
        second = self.reconciler.reconcile(TRIGGER_EXPLICIT)
        self.assertIn(self.location(digest), second.deleted)
        self.assertFalse(self.exists(digest))

    def test_t5_stale_staging_variants_are_collected_after_grace(self):
        empty = self.staging("ab", files=(), age_ms=2 * DAY_MS)
        payload_only = self.staging("ab", files=("payload",), age_ms=2 * DAY_MS)
        both = self.staging("cd", age_ms=2 * DAY_MS)
        recent = self.staging("cd", age_ms=HOUR_MS)
        extra = self.staging("ef", files=("payload", "other.txt"), age_ms=2 * DAY_MS)
        linked = self.root / "ef" / ".pending-linked"
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        try:
            make_directory_link(linked, outside)
        except OSError:
            linked = None
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        self.assertEqual(len(plan.stale_staging), 3)
        report = self.reconciler.apply(plan)
        self.assertEqual(len(report.deleted), 3)
        for gone in (empty, payload_only, both):
            self.assertFalse(gone.exists(), gone)
        self.assertTrue(recent.exists() and extra.exists())
        self.assertTrue((outside / "keep.txt").exists())
        reasons = dict(report.corrupt)
        self.assertEqual(reasons[f"ef/{extra.name}"], "unexpected_contents")
        if linked is not None:
            self.assertEqual(reasons[f"ef/{linked.name}"], "not_plain_directory")

    def test_t6_object_registered_between_plan_and_apply_is_kept(self):
        digest = self.orphan("a")
        other = self.orphan("b")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        self.collector.register(self.reference(digest), entry_id="late")
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(report.skipped, ((self.location(digest), "indexed"),))
        self.assertEqual(report.deleted, (self.location(other),))
        self.assertNotEqual(report.plan_version, report.applied_version)
        self.assertTrue(self.exists(digest))

    def test_t6_new_pending_or_live_between_plan_and_apply_is_kept(self):
        pending = self.orphan("a")
        live = self.orphan("b")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        self.pending_record(pending)
        self.source.references = (self.reference(live),)
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(
            dict(report.skipped),
            {self.location(pending): "pending", self.location(live): "live"},
        )
        self.assertEqual(report.deleted, ())


class IntegrityTest(ReconcilerCase):
    def test_t7_corrupt_objects_are_never_deleted_and_are_reported(self):
        cases = {}
        cases["bad_metadata"] = self.orphan("a")
        (self.store.object_path(cases["bad_metadata"]) / "metadata.json").write_text(
            "{"
        )
        cases["extra_file"] = self.orphan("b")
        (self.store.object_path(cases["extra_file"]) / "note.txt").write_text("x")
        cases["size_mismatch"] = self.orphan("c")
        with open(
            self.store.object_path(cases["size_mismatch"]) / "payload", "ab"
        ) as f:
            f.write(b"+")
        cases["digest_mismatch"] = self.orphan("d")
        metadata_path = (
            self.store.object_path(cases["digest_mismatch"]) / "metadata.json"
        )
        metadata = json.loads(metadata_path.read_text())
        metadata["sha256"] = "1" * 64
        metadata_path.write_text(json.dumps(metadata))
        cases["payload_changed"] = self.orphan("e")
        payload_path = self.store.object_path(cases["payload_changed"]) / "payload"
        payload_path.write_bytes(b"z" * 20)
        self.set_activity(cases["payload_changed"], 2 * DAY_MS)
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(report.deleted, ())
        reported = dict(report.corrupt)
        for name, digest in cases.items():
            self.assertIn(self.location(digest), reported, name)
            self.assertTrue(self.exists(digest), name)
        self.assertEqual(
            reported[self.location(cases["payload_changed"])], "payload_digest_mismatch"
        )

    def test_t7_links_are_reported_not_followed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        real = self.orphan("a")
        link_name = f"{real[:2]}{'1' * 62}"
        try:
            make_directory_link(self.root / "ee", outside)
            make_directory_link(self.root / real[:2] / link_name, outside)
        except OSError:
            self.skipTest("directory links are not available")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        states = self.candidates(plan)
        self.assertEqual(states["ee"][0], STATE_CORRUPT_OR_UNKNOWN)
        self.assertEqual(states[f"{real[:2]}/{link_name}"][0], STATE_CORRUPT_OR_UNKNOWN)
        self.reconciler.apply(plan, recover_pending=False)
        self.assertTrue((outside / "keep.txt").exists())


class AuditAndCrashTest(ReconcilerCase):
    def test_t8_stop_points_recover_and_audit_each_deletion_once(self):
        digest = self.orphan("a", size=30)
        reconciler = self.make_reconciler(rmtree=self.failing_rmtree)
        report = reconciler.reconcile(TRIGGER_EXPLICIT)
        self.assertEqual(report.deleted, (self.location(digest),))
        self.assertFalse(self.exists(digest))
        self.assertGreater(report.trash_residual_bytes, 0)
        # 次回は掃除が終わり、auditは1件だけ（今回分はrecovered=False）。
        second = self.make_reconciler()
        again = second.reconcile(TRIGGER_EXPLICIT)
        self.assertEqual(again.trash_swept, 1)
        self.assertEqual(again.trash_residual_bytes, 0)
        (audit,) = again.audits
        self.assertTrue(audit.recovered)
        self.assertEqual((audit.sha256, audit.artifact_type), (digest, ARTIFACT_TYPE))
        self.assertEqual(audit.reason, "safe_orphan")
        self.assertEqual(self.collector.load_entries(), ())
        second.acknowledge([audit.audit_id])
        self.assertEqual(second.pending_audits(), ())
        self.assertEqual(
            [p.name for p in (self.root / TRASH_DIRECTORY_NAME).glob("*.intent.json")],
            [],
        )

    @staticmethod
    def failing_rmtree(path):
        raise OSError("injected rmtree stop")

    def test_t8_collect_and_recover_pending_leave_no_half_object(self):
        class TrashStopFileSystem(LocalRetentionFileSystem):
            def remove_object(self, path):
                ArtifactTrash(
                    path.parent.parent, rmtree=AuditAndCrashTest.failing_rmtree
                ).discard(
                    path,
                    kind=KIND_OBJECT,
                    sha256=path.name,
                    reason=REASON_RETENTION_REMOVE,
                )

        collector = self.make_collector(
            RetentionPolicy(1, 100_000), TrashStopFileSystem()
        )
        reference = self.reference(self.put("a"))
        collector.register(reference, entry_id="a")
        self.clock.now_ms += 1  # 期限なしでもsoft budget超過でGCされる
        collector.collect()
        self.assertFalse(self.exists(reference.sha256))
        self.assertGreater(collector.measure().usage.trash_bytes, 0)
        # recover_pendingも同じ経路でshardに半壊objectを残さない。
        digest = self.put("b")
        self.pending_record(digest)
        ArtifactAdmissionCoordinator(collector).recover_pending()
        self.assertFalse(self.exists(digest))
        self.assertEqual(
            self.make_reconciler().reconcile(TRIGGER_EXPLICIT).trash_residual_bytes, 0
        )

    def test_t11_report_and_audit_contents_have_no_paths(self):
        self.orphan("a", size=33)
        self.staging("ab", age_ms=2 * DAY_MS)
        report = self.reconciler.reconcile(TRIGGER_EXPLICIT)
        self.assertEqual(len(report.audits), 2)
        kinds = {audit.kind: audit for audit in report.audits}
        audit = kinds["object"]
        self.assertEqual(audit.data_class, "raw_frame")
        self.assertEqual(audit.trigger, TRIGGER_EXPLICIT)
        self.assertFalse(audit.recovered)
        self.assertGreaterEqual(audit.size_bytes, 33)
        document = audit.to_dict()
        json.dumps(document)
        self.assertNotIn(str(self.root), json.dumps(document))
        self.assertEqual(kinds["staging"].sha256, None)
        self.assertEqual(report.deleted_bytes, sum(a.size_bytes for a in report.audits))


class DryRunTest(ReconcilerCase):
    def test_t10_plan_changes_nothing_and_missing_root_creates_nothing(self):
        missing = Path(self.temp.name) / "missing"
        store = FileSystemArtifactStore(missing)
        collector = RetentionCollector(
            store, RetentionPolicy(100, 100), clock=self.clock
        )
        reconciler = OrphanReconciler(
            collector, ArtifactAdmissionCoordinator(collector), [self.source]
        )
        plan = reconciler.plan(TRIGGER_EXPLICIT)
        self.assertEqual(
            (plan.candidates, plan.targets, plan.blocked_reason), ((), (), None)
        )
        self.assertFalse(missing.exists())
        self.orphan("a")
        self.register("b")
        self.staging("ab", age_ms=2 * DAY_MS)
        self.reconciler.reconcile(TRIGGER_EXPLICIT)  # trash/cursorを作っておく
        self.orphan("c")
        before = tree_listing(self.root)
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        dry = self.reconciler.reconcile(TRIGGER_EXPLICIT, dry_run=True)
        self.assertEqual(before, tree_listing(self.root))
        self.assertEqual(dry.deleted, ())
        self.assertEqual(len(plan.targets), 1)

    def test_t10_unchanged_apply_matches_plan_and_changed_apply_is_a_subset(self):
        a = self.orphan("a")
        b = self.orphan("b")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(plan.state_version, report.applied_version)
        self.assertEqual(set(report.deleted), {t.location for t in plan.targets})
        c = self.orphan("c")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        self.assertEqual([t.sha256 for t in plan.targets], [c])
        late = self.orphan("d")  # planの後に増えたorphanは追加されない
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(report.deleted, (self.location(c),))
        self.assertTrue(self.exists(late))
        self.assertFalse(self.exists(a) or self.exists(b))


class BudgetTest(ReconcilerCase):
    soft = 200
    hard = 2000

    def test_t9_orphan_over_budget_is_reclaimed_without_touching_indexed_entries(self):
        kept = self.register("a")
        orphan = self.orphan("b", size=400)
        report = reclaim_budget(self.reconciler)
        self.assertFalse(self.exists(orphan))
        self.assertTrue(self.exists(kept))
        self.assertEqual(len(self.collector.load_entries()), 1)
        self.assertIsNone(report.blocked_reason)
        self.assertEqual(report.retention.deleted_artifacts, ())
        self.assertEqual(len(report.reconcile.deleted), 1)

    def test_t9_unreclaimable_orphan_blocks_collect_with_orphan_pressure(self):
        kept = self.register("a")
        recent = self.orphan("b", size=400, age_ms=HOUR_MS)
        report = reclaim_budget(self.reconciler)
        self.assertEqual(report.blocked_reason, "orphan_pressure")
        self.assertIsNone(report.retention)
        self.assertGreater(report.remaining_unindexed_bytes, 0)
        self.assertTrue(self.exists(kept) and self.exists(recent))
        self.assertEqual(len(self.collector.load_entries()), 1)

    def test_t9_no_orphan_pressure_when_live_entry_is_kept_by_both_plans(self):
        kept = self.register("a")
        self.source.references = (self.reference(kept),)
        self.orphan("b", size=400, age_ms=HOUR_MS)
        report = reclaim_budget(self.reconciler)
        # entryはliveで保護される。orphan分の上乗せで消えるentryは無いのでcollectされる。
        self.assertIsNone(report.blocked_reason)
        self.assertIsNotNone(report.retention)
        self.assertTrue(self.exists(kept))

    def test_t9_incomplete_live_blocks_reclaim(self):
        self.orphan("b", size=400)
        reconciler = self.make_reconciler(live_sources=[StubSource(complete=False)])
        report = reclaim_budget(reconciler)
        self.assertEqual(report.blocked_reason, "live_snapshot_incomplete")
        self.assertIsNone(report.retention)


class BoundaryTest(ReconcilerCase):
    def test_t13_config_and_construction_validation(self):
        for kwargs in (
            {"grace_ms": 59_999},
            {"grace_ms": 31 * DAY_MS},
            {"max_candidates": 0},
            {"max_delete_count": 0},
            {"max_lock_hold_ms": 0},
            {"future_skew_ms": -1},
            {"live_snapshot_timeout_ms": True},
        ):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                OrphanReconcilerConfig(**kwargs)
        with self.assertRaises(ValueError):
            OrphanReconciler(self.collector, self.admission, [])
        OrphanReconciler(
            self.collector, self.admission, [], declared_no_live_sources=True
        )
        with self.assertRaises(ValueError):
            self.reconciler.plan("nope")

    def test_adapter_reference_snapshot_is_accepted_as_a_live_source(self):
        from ai_game_player.runtime.retention_adapter import RetentionReferenceSnapshot

        protected = self.orphan("a")
        other = self.orphan("b")

        class AdapterProvider:
            def __init__(self, references, complete):
                self.references = references
                self.complete = complete

            def snapshot(self):
                return RetentionReferenceSnapshot(
                    session_id="s",
                    generation=0,
                    created_at_ms=0,
                    artifact_sha256s=frozenset(r.sha256 for r in self.references),
                    references=self.references,
                    source_positions={},
                    complete=self.complete,
                    incomplete_reasons=() if self.complete else ("journal",),
                )

        reference = self.reference(protected)
        blocked = self.make_reconciler(
            live_sources=[AdapterProvider((reference,), False)]
        )
        self.assertIsNotNone(blocked.plan(TRIGGER_EXPLICIT).blocked_reason)
        reconciler = self.make_reconciler(
            live_sources=[AdapterProvider((reference,), True)]
        )
        report = reconciler.reconcile(TRIGGER_EXPLICIT)
        self.assertEqual(report.deleted, (self.location(other),))
        self.assertTrue(self.exists(protected))

    def test_t13_periodic_is_rate_limited(self):
        self.orphan("a")
        first = self.reconciler.reconcile(TRIGGER_PERIODIC)
        self.assertIsNone(first.blocked_reason)
        self.orphan("b")
        second = self.reconciler.reconcile(TRIGGER_PERIODIC)
        self.assertEqual(second.blocked_reason, "rate_limited")
        self.clock.now_ms += 11 * 60_000
        self.assertIsNone(self.reconciler.reconcile(TRIGGER_PERIODIC).blocked_reason)

    def test_t13_max_delete_count_is_not_refilled_and_counts_staging(self):
        digests = [self.orphan(label) for label in "abc"]
        self.staging("ab", age_ms=2 * DAY_MS)
        config = OrphanReconcilerConfig(max_delete_count=2)
        reconciler = self.make_reconciler(config=config)
        plan = reconciler.plan(TRIGGER_EXPLICIT)
        self.assertEqual(len(plan.targets), 2)
        # 1件が外れても補充しない。
        victim = plan.targets[0].sha256
        self.collector.register(self.reference(victim), entry_id="late")
        report = reconciler.apply(plan, recover_pending=False)
        self.assertEqual(len(report.deleted), 1)
        self.assertEqual(sum(self.exists(d) for d in digests), 2)

    def test_t13_cursor_reaches_later_orphans_over_several_runs(self):
        digests = sorted(self.orphan(label) for label in "abcdef")
        config = OrphanReconcilerConfig(max_candidates=2)
        reconciler = self.make_reconciler(config=config)
        deleted: set[str] = set()
        for _ in range(3):
            report = reconciler.reconcile(TRIGGER_EXPLICIT)
            deleted.update(report.deleted)
        self.assertEqual(deleted, {self.location(d) for d in digests})

    def test_t13_verification_budget_defers_but_first_item_is_always_verified(self):
        for label in "ab":
            self.orphan(label, size=100)
        config = OrphanReconcilerConfig(max_verify_bytes_per_run=10)
        reconciler = self.make_reconciler(config=config)
        plan = reconciler.plan(TRIGGER_EXPLICIT)
        self.assertEqual(len(plan.targets), 1)
        reasons = [c.reason for c in plan.candidates]
        self.assertIn("verification_deferred", reasons)
        report = reconciler.apply(plan, recover_pending=False)
        self.assertEqual(len(report.deleted), 1)
        # 次回の走査で残りも検証される。
        self.assertEqual(len(reconciler.reconcile(TRIGGER_EXPLICIT).deleted), 1)

    def test_t13_recreated_object_with_same_size_is_detected_by_fingerprint(self):
        digest = self.orphan("a")
        plan = self.reconciler.plan(TRIGGER_EXPLICIT)
        path = self.store.object_path(digest)
        payload = (path / "payload").read_bytes()
        metadata = (path / "metadata.json").read_bytes()
        for item in (path / "payload", path / "metadata.json"):
            item.unlink()
        path.rmdir()
        path.mkdir()
        (path / "payload").write_bytes(payload)
        (path / "metadata.json").write_bytes(metadata)
        self.set_activity(digest, 2 * DAY_MS)
        report = self.reconciler.apply(plan, recover_pending=False)
        self.assertEqual(
            report.skipped, ((self.location(digest), "changed_since_plan"),)
        )
        self.assertTrue(self.exists(digest))

    def test_t13_lock_hold_budget_stops_remaining_deletions(self):
        for label in "abc":
            self.orphan(label)
        ticks = iter(range(0, 1000))
        reconciler = self.make_reconciler(
            config=OrphanReconcilerConfig(max_lock_hold_ms=1000),
            timer=lambda: next(ticks) * 0.6,
        )
        plan = reconciler.plan(TRIGGER_EXPLICIT)
        report = reconciler.apply(plan, recover_pending=False)
        self.assertTrue(report.stopped_by_lock_budget)
        self.assertLess(len(report.deleted), 3)

    def test_t14_trash_that_is_a_link_or_file_stops_everything(self):
        digest = self.orphan("a")
        (self.root / TRASH_DIRECTORY_NAME).write_text("file")
        with self.assertRaises(RetentionRootUnsafeError):
            self.reconciler.plan(TRIGGER_EXPLICIT)
        self.assertTrue(self.exists(digest))

    def test_t14_residual_trash_counts_toward_hard_limit(self):
        digest = self.orphan("a", size=50)
        reconciler = self.make_reconciler(rmtree=AuditAndCrashTest.failing_rmtree)
        reconciler.reconcile(TRIGGER_EXPLICIT)
        usage = self.collector.measure().usage
        self.assertGreater(usage.trash_bytes, 50)
        self.assertEqual(usage.total_bytes, usage.trash_bytes)
        self.assertFalse(self.exists(digest))
        request = ArtifactAdmissionRequest(
            payload_size=self.hard - usage.trash_bytes + 1,
            expected_sha256=None,
            artifact_type=ARTIFACT_TYPE,
            media_type=MEDIA_TYPE,
            entry_id="big",
            protected=False,
            source_ref="test.source",
            sensitive=False,
        )
        from ai_game_player.retention_collector import RetentionCapacityError

        with self.assertRaises(RetentionCapacityError):
            self.admission.admit(
                request, lambda port: port.put(b"x" * request.payload_size)
            )


class ConcurrencyTest(ReconcilerCase):
    def test_t12_concurrent_admit_and_collect_never_lose_registered_objects(self):
        self.collector = self.make_collector(timeout=30.0)
        self.admission = ArtifactAdmissionCoordinator(self.collector)
        reconciler = self.make_reconciler(
            collector=self.collector, admission=self.admission
        )
        for label in "xyz":
            self.orphan(label)
        admitted: list[str] = []
        errors: list[BaseException] = []

        def admit_many() -> None:
            try:
                for number in range(8):
                    payload = f"payload-{number}".encode("ascii") * 3
                    request = ArtifactAdmissionRequest(
                        payload_size=len(payload),
                        expected_sha256=hashlib.sha256(payload).hexdigest(),
                        artifact_type=ARTIFACT_TYPE,
                        media_type=MEDIA_TYPE,
                        entry_id=f"admit-{number}",
                        protected=True,
                        source_ref="test.source",
                        sensitive=False,
                    )
                    admitted.append(
                        self.admission.admit(
                            request, lambda port, p=payload: port.put(p)
                        ).sha256
                    )
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        thread = threading.Thread(target=admit_many)
        thread.start()
        for _ in range(4):
            reconciler.reconcile(TRIGGER_EXPLICIT)
            reclaim_budget(reconciler)
        thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(len(admitted), 8)
        for digest in admitted:
            self.assertTrue(self.exists(digest))
        self.assertLessEqual(self.collector.measure().usage.total_bytes, self.hard)

    def test_t12_slow_live_source_does_not_hold_the_root_lock(self):
        self.source.delay = 0.5
        self.orphan("a")
        started = threading.Event()
        result: list[float] = []

        def run_plan() -> None:
            started.set()
            self.reconciler.plan(TRIGGER_EXPLICIT)

        thread = threading.Thread(target=run_plan)
        thread.start()
        started.wait()
        time.sleep(0.1)
        began = time.monotonic()
        with self.collector.coordination_lock():
            result.append(time.monotonic() - began)
        thread.join()
        self.assertLess(result[0], 0.3)


if __name__ == "__main__":
    unittest.main()
