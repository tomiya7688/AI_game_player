import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_game_player import artifact_trash
from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.artifact_trash import (
    KIND_OBJECT,
    REASON_RETENTION_REMOVE,
    REASON_SAFE_ORPHAN,
    STATE_RENAMED,
    STATE_SWEPT,
    TRASH_DIRECTORY_NAME,
    ArtifactTrash,
    TrashIntent,
    TrashIntentError,
)
from ai_game_player.retention_collector import (
    LocalRetentionFileSystem,
    RetentionCapacityError,
    RetentionCollector,
)
from ai_game_player.retention_index import RetentionIndexError
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.retention_root_lock import RetentionRootUnsafeError
from tests.test_retention_root_lock import make_directory_link

MEDIA_TYPE = "application/octet-stream"


def failing_rmtree(path):
    raise OSError("injected rmtree stop")


def make_intent(intent_id: str, digest: str, **overrides) -> TrashIntent:
    values = dict(
        id=intent_id,
        kind=KIND_OBJECT,
        sha256=digest,
        source=f"{digest[:2]}/{digest}",
        size_bytes=10,
        artifact_type=None,
        sensitive=None,
        data_class=None,
        reason=REASON_SAFE_ORPHAN,
        trigger=None,
        state="pending",
    )
    values.update(overrides)
    return TrashIntent(**values)


class TrashTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        self.store = FileSystemArtifactStore(self.root)

    def put(self, payload: bytes) -> tuple[str, Path]:
        reference = self.store.put(
            payload, media_type=MEDIA_TYPE, artifact_type="frame.raw"
        )
        return reference.sha256, self.store.object_path(reference.sha256)

    def discard(self, trash: ArtifactTrash, digest: str, path: Path, **kwargs):
        return trash.discard(
            path,
            kind=KIND_OBJECT,
            sha256=digest,
            reason=kwargs.pop("reason", REASON_SAFE_ORPHAN),
            **kwargs,
        )

    def entries(self) -> list[str]:
        directory = self.root / TRASH_DIRECTORY_NAME
        return sorted(item.name for item in directory.iterdir())


class DiscardTest(TrashTestCase):
    def test_discard_removes_object_and_keeps_audit_intent_until_ack(self):
        digest, path = self.put(b"a" * 10)
        trash = ArtifactTrash(self.root)
        intent = self.discard(trash, digest, path, trigger="explicit")
        self.assertFalse(path.exists())
        self.assertEqual(intent.state, STATE_SWEPT)
        (kept,) = trash.list_intents()
        self.assertEqual((kept.id, kept.state), (intent.id, STATE_SWEPT))
        self.assertEqual(trash.residual_bytes(), 0)
        self.assertEqual(trash.audit_intents(), (kept,))
        trash.acknowledge([intent.id])
        self.assertEqual(trash.list_intents(), ())
        self.assertEqual(self.entries(), [])

    def test_retention_remove_intent_is_deleted_after_sweep(self):
        digest, path = self.put(b"b" * 10)
        trash = ArtifactTrash(self.root)
        self.discard(trash, digest, path, reason=REASON_RETENTION_REMOVE)
        self.assertEqual(self.entries(), [])

    def test_ack_before_sweep_keeps_intent_for_the_sweep(self):
        digest, path = self.put(b"c" * 10)
        trash = ArtifactTrash(self.root, rmtree=failing_rmtree)
        intent = self.discard(trash, digest, path)
        trash.acknowledge([intent.id])
        self.assertEqual(trash.audit_intents(), ())
        report = ArtifactTrash(self.root).sweep()
        self.assertEqual(report.swept, (intent.id,))
        self.assertEqual(self.entries(), [])

    def test_rename_failure_removes_intent_and_keeps_source(self):
        digest, path = self.put(b"d" * 10)
        trash = ArtifactTrash(self.root)
        with mock.patch.object(artifact_trash.os, "rename", side_effect=OSError("x")):
            with self.assertRaises(OSError):
                self.discard(trash, digest, path)
        self.assertTrue(path.is_dir())
        self.assertEqual(trash.list_intents(), ())

    def test_source_outside_root_shard_is_rejected(self):
        digest, path = self.put(b"e" * 10)
        other = ArtifactTrash(Path(self.temp.name) / "other")
        with self.assertRaises(ValueError):
            self.discard(other, digest, path)
        self.assertTrue(path.is_dir())

    def test_intent_limit_rejects_new_discard(self):
        digest, path = self.put(b"f" * 10)
        trash = ArtifactTrash(self.root)
        with mock.patch.object(artifact_trash, "MAX_TRASH_INTENTS", 0):
            with self.assertRaises(RetentionCapacityError):
                self.discard(trash, digest, path)
        self.assertTrue(path.is_dir())


class ReviewFixTest(TrashTestCase):
    def test_exactly_max_intents_are_listed_and_one_more_is_rejected(self):
        trash = ArtifactTrash(self.root)
        trash.ensure()
        for number in range(3):
            trash._write_intent(make_intent(f"{number}" * 32, "a" * 64))
        with mock.patch.object(artifact_trash, "MAX_TRASH_INTENTS", 3):
            self.assertEqual(len(trash.list_intents()), 3)
        with mock.patch.object(artifact_trash, "MAX_TRASH_INTENTS", 2):
            with self.assertRaises(TrashIntentError):
                trash.list_intents()

    def test_unreadable_tree_stops_before_rename(self):
        digest, path = self.put(b"o" * 10)
        trash = ArtifactTrash(self.root)
        with mock.patch.object(artifact_trash.os, "lstat", side_effect=OSError("x")):
            with self.assertRaises(OSError):
                self.discard(trash, digest, path)
        self.assertTrue(path.is_dir())

    def test_intent_update_failure_after_rename_is_not_reported_as_failure(self):
        digest, path = self.put(b"p" * 10)
        trash = ArtifactTrash(self.root)
        real = trash._write_intent
        calls = []

        def flaky(intent):
            calls.append(intent.state)
            if intent.state == STATE_RENAMED:
                raise OSError("injected")
            real(intent)

        trash._write_intent = flaky
        intent = self.discard(trash, digest, path)
        self.assertEqual(intent.state, "pending")
        self.assertFalse(path.exists())
        trash._write_intent = real
        self.assertEqual(trash.sweep().swept, (intent.id,))
        self.assertEqual(trash.audit_intents()[0].state, STATE_SWEPT)


class CrashRecoveryTest(TrashTestCase):
    def test_stop_after_intent_before_rename_is_abandoned(self):
        digest, path = self.put(b"g" * 10)
        trash = ArtifactTrash(self.root)
        trash.ensure()
        intent = make_intent("1" * 32, digest)
        trash._write_intent(intent)
        report = trash.sweep()
        self.assertEqual(report.abandoned, (intent.id,))
        self.assertTrue(path.is_dir())
        self.assertEqual(trash.list_intents(), ())

    def test_stop_after_rename_before_renamed_update_is_resumed(self):
        digest, path = self.put(b"h" * 10)
        trash = ArtifactTrash(self.root)
        trash.ensure()
        intent = make_intent("2" * 32, digest, trigger="startup")
        trash._write_intent(intent)
        os.rename(path, trash.directory / intent.id)
        report = trash.sweep()
        self.assertEqual(report.swept, (intent.id,))
        (kept,) = trash.audit_intents()
        self.assertEqual(kept.state, STATE_SWEPT)
        self.assertEqual(self.entries(), [f"{intent.id}.intent.json"])

    def test_rmtree_stop_leaves_no_half_object_and_counts_residual(self):
        digest, path = self.put(b"i" * 10)
        trash = ArtifactTrash(self.root, rmtree=failing_rmtree)
        intent = self.discard(trash, digest, path)
        self.assertFalse(path.exists())
        self.assertGreater(trash.residual_bytes(), 0)
        self.assertIn(intent.id, self.entries())
        resumed = ArtifactTrash(self.root)
        self.assertEqual(resumed.sweep().swept, (intent.id,))
        self.assertEqual(resumed.residual_bytes(), 0)
        self.assertEqual(self.entries(), [f"{intent.id}.intent.json"])

    def test_entries_without_intent_or_failing_checks_are_never_removed(self):
        digest, path = self.put(b"j" * 10)
        trash = ArtifactTrash(self.root)
        trash.ensure()
        orphan_entry = trash.directory / ("3" * 32)
        orphan_entry.mkdir()
        (orphan_entry / "payload").write_bytes(b"x")
        report = trash.sweep()
        self.assertIn(("3" * 32, "entry_without_intent"), report.diagnostics)
        self.assertTrue(orphan_entry.is_dir())
        # extra fileを含む・digestが違うentryはintentがあっても消さない。
        for number, (extra, wrong_digest) in enumerate(
            [("note.txt", False), (None, True)]
        ):
            name = f"{number + 4}" * 32
            entry = trash.directory / name
            entry.mkdir()
            (entry / "payload").write_bytes(b"y")
            if extra:
                (entry / extra).write_bytes(b"z")
            if wrong_digest:
                metadata = json.loads((path / "metadata.json").read_text())
                metadata["sha256"] = "0" * 64
                (entry / "metadata.json").write_text(json.dumps(metadata))
            trash._write_intent(
                make_intent(name, digest, state="renamed", deleted_at_ms=1)
            )
            report = trash.sweep()
            self.assertTrue(entry.is_dir(), name)
            self.assertIn(name, [item[0] for item in report.diagnostics])

    def test_sweep_stops_at_deadline(self):
        digest, path = self.put(b"k" * 10)
        trash = ArtifactTrash(self.root, rmtree=failing_rmtree)
        self.discard(trash, digest, path)
        report = ArtifactTrash(self.root).sweep(deadline=0.0, timer=lambda: 1.0)
        self.assertTrue(report.stopped_by_deadline)
        self.assertEqual(report.swept, ())


class SafetyTest(TrashTestCase):
    def test_trash_that_is_a_link_is_refused_without_writing(self):
        digest, path = self.put(b"l" * 10)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            make_directory_link(self.root / TRASH_DIRECTORY_NAME, outside)
        except OSError:
            self.skipTest("directory links are not available")
        trash = ArtifactTrash(self.root)
        with self.assertRaises(RetentionRootUnsafeError):
            self.discard(trash, digest, path)
        self.assertTrue(path.is_dir())
        self.assertEqual(list(outside.iterdir()), [])

    def test_trash_that_is_a_file_is_refused(self):
        digest, path = self.put(b"m" * 10)
        (self.root / TRASH_DIRECTORY_NAME).write_text("not a directory")
        with self.assertRaises(RetentionRootUnsafeError):
            self.discard(ArtifactTrash(self.root), digest, path)
        self.assertTrue(path.is_dir())

    def test_corrupt_intent_fails_closed_and_blocks_measure(self):
        trash = ArtifactTrash(self.root)
        trash.ensure()
        (trash.directory / f"{'5' * 32}.intent.json").write_text("{broken")
        with self.assertRaises(TrashIntentError):
            trash.list_intents()
        collector = RetentionCollector(self.store, RetentionPolicy(100, 100))
        with self.assertRaises(RetentionIndexError):
            collector.measure()

    def test_intent_rejects_unknown_fields_and_bad_values(self):
        good = make_intent("6" * 32, "a" * 64)
        document = good.to_dict()
        self.assertEqual(TrashIntent.from_dict(document), good)
        for mutate in (
            lambda d: d.update(extra=1),
            lambda d: d.update(size_bytes=-1),
            lambda d: d.update(source="../x"),
            lambda d: d.update(state="done"),
            lambda d: d.update(schema_version=2),
        ):
            broken = dict(document)
            mutate(broken)
            with self.assertRaises(TrashIntentError):
                TrashIntent.from_dict(broken)


class UsageTest(TrashTestCase):
    def test_residual_trash_bytes_count_toward_usage_and_hard_limit(self):
        class TrashFileSystem(LocalRetentionFileSystem):
            def remove_object(self, path):
                ArtifactTrash(path.parent.parent, rmtree=failing_rmtree).discard(
                    path,
                    kind=KIND_OBJECT,
                    sha256=path.name,
                    reason=REASON_RETENTION_REMOVE,
                )

        digest, path = self.put(b"n" * 40)
        collector = RetentionCollector(
            self.store, RetentionPolicy(1000, 1000), file_system=TrashFileSystem()
        )
        collector.file_system.remove_object(path)
        usage = collector.measure().usage
        self.assertGreater(usage.trash_bytes, 0)
        self.assertEqual(usage.total_bytes, usage.trash_bytes)
        room = 1000 - usage.trash_bytes
        self.assertTrue(collector.can_admit(room))
        self.assertFalse(collector.can_admit(room + 1))


if __name__ == "__main__":
    unittest.main()
