import json
import os
import tempfile
import unittest
from pathlib import Path

from ai_game_player.artifact_store import FileSystemArtifactStore
from unittest import mock

from ai_game_player import retention_collector
from ai_game_player.retention_collector import (
    RETENTION_INDEX_FILENAME,
    LocalRetentionFileSystem,
    RetentionCapacityError,
    RetentionCollector,
    RetentionSafetyError,
)
from ai_game_player.retention_index import (
    RetentionEntry,
    RetentionIndexError,
    parse_retention_index,
    retention_index_document,
)
from ai_game_player.retention_planner import plan_retention
from ai_game_player.retention_policy import RetentionPolicy, RetentionPolicyError

CONFIG = Path(__file__).parents[1] / "config"


def digest(label: str) -> str:
    return (label * 64)[:64]


def entry(
    entry_id: str,
    sha: str,
    *,
    size: int = 10,
    created: int = 0,
    expires: int | None = None,
    protected: bool = False,
) -> RetentionEntry:
    return RetentionEntry(entry_id, sha, size, "frame.raw", created, expires, protected)


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class RecordingFileSystem(LocalRetentionFileSystem):
    def __init__(self) -> None:
        self.removed: list[Path] = []
        self.index_writes = 0

    def write_index(self, path, document):
        self.index_writes += 1
        super().write_index(path, document)

    def remove_object(self, path):
        self.removed.append(path)
        super().remove_object(path)


class RetentionPlannerTest(unittest.TestCase):
    def policy(self, soft: int = 1_000, hard: int = 2_000) -> RetentionPolicy:
        return RetentionPolicy(soft_budget_bytes=soft, hard_limit_bytes=hard)

    def test_budget_evicts_oldest_unprotected_first_with_entry_id_tiebreak(self):
        entries = [
            entry("c", digest("c"), created=5),
            entry("b", digest("b"), created=1),
            entry("a", digest("a"), created=1),
            entry("old-protected", digest("d"), created=0, protected=True),
        ]
        plan = plan_retention(entries, self.policy(soft=20, hard=40), now_ms=10)

        self.assertEqual(["a", "b"], [item.entry_id for item in plan.removed_entries])
        self.assertEqual({"budget"}, {item.reason for item in plan.removed_entries})
        self.assertEqual(
            ["c", "old-protected"], [item.entry_id for item in plan.kept_entries]
        )
        self.assertEqual((digest("a"), digest("b")), plan.deleted_artifacts)
        self.assertEqual((40, 20), (plan.bytes_before, plan.bytes_after))
        self.assertFalse(plan.over_budget)

    def test_protected_entries_are_never_removed_even_when_over_hard_limit(self):
        entries = [
            entry("p1", digest("a"), size=50, protected=True, expires=1),
            entry("p2", digest("a"), size=50, protected=True),
            entry("p3", digest("b"), size=50, protected=True),
        ]
        plan = plan_retention(entries, self.policy(soft=10, hard=20), now_ms=999)

        self.assertEqual((), plan.removed_entries)
        self.assertEqual((), plan.deleted_artifacts)
        self.assertTrue(plan.over_budget)
        self.assertTrue(plan.over_hard_limit)

    def test_ttl_expires_unprotected_entries_independent_of_budget(self):
        entries = [
            entry("live", digest("a"), expires=100),
            entry("later", digest("b"), expires=101),
            entry("forever", digest("c")),
        ]
        plan = plan_retention(entries, self.policy(), now_ms=100)

        self.assertEqual(
            [("live", "expired")],
            [(i.entry_id, i.reason) for i in plan.removed_entries],
        )
        self.assertEqual((digest("a"),), plan.deleted_artifacts)

    def test_expired_but_externally_referenced_entry_is_blocked_not_removed(self):
        entries = [entry("referenced", digest("a"), expires=1)]
        plan = plan_retention(
            entries, self.policy(), now_ms=5, live_artifact_sha256={digest("a")}
        )

        self.assertEqual((), plan.removed_entries)
        self.assertEqual("expired_but_referenced", plan.blocked_entries[0].reason)
        self.assertEqual((), plan.deleted_artifacts)

    def test_exact_duplicates_collapse_to_oldest_and_keep_artifact(self):
        entries = [
            entry("dup-2", digest("a"), created=2),
            entry("dup-1", digest("a"), created=1),
            entry("dup-3", digest("a"), created=1),
        ]
        plan = plan_retention(entries, self.policy(), now_ms=10)

        self.assertEqual(["dup-1"], [item.entry_id for item in plan.kept_entries])
        self.assertEqual(
            [("dup-3", "dup-1"), ("dup-2", "dup-1")],
            [(item.entry_id, item.alias_of) for item in plan.removed_entries],
        )
        self.assertEqual((), plan.deleted_artifacts)
        self.assertEqual((10, 10), (plan.bytes_before, plan.bytes_after))

    def test_duplicate_survivor_is_the_longest_retained_entry(self):
        entries = [
            entry("old", digest("a"), created=0, expires=5),
            entry("new", digest("a"), created=1, expires=50),
        ]
        plan = plan_retention(entries, self.policy(), now_ms=10)

        self.assertEqual(["new"], [item.entry_id for item in plan.kept_entries])
        self.assertEqual(
            [("old", "duplicate", "new")],
            [(i.entry_id, i.reason, i.alias_of) for i in plan.removed_entries],
        )
        self.assertEqual((), plan.deleted_artifacts)
        forever = plan_retention(
            [*entries, entry("forever", digest("a"), created=2)],
            self.policy(),
            now_ms=10,
        )
        self.assertEqual(["forever"], [item.entry_id for item in forever.kept_entries])

    def test_duplicates_prefer_protected_survivors(self):
        entries = [
            entry("plain", digest("a"), created=0),
            entry("keep", digest("a"), created=9, protected=True),
        ]
        plan = plan_retention(entries, self.policy(), now_ms=10)

        self.assertEqual(["keep"], [item.entry_id for item in plan.kept_entries])
        self.assertEqual("keep", plan.removed_entries[0].alias_of)

    def test_live_references_are_not_evicted_for_budget(self):
        entries = [
            entry("a", digest("a"), size=30),
            entry("b", digest("b"), size=30, created=1),
        ]
        plan = plan_retention(
            entries,
            self.policy(soft=30, hard=60),
            now_ms=10,
            live_artifact_sha256=[digest("a")],
        )

        self.assertEqual(["b"], [item.entry_id for item in plan.removed_entries])
        self.assertNotIn(digest("a"), plan.deleted_artifacts)

    def test_unindexed_bytes_count_toward_budget_but_are_never_deleted(self):
        entries = [entry("a", digest("a"), size=30)]
        plan = plan_retention(
            entries, self.policy(soft=50, hard=60), now_ms=10, unindexed_bytes=40
        )

        self.assertEqual(["a"], [item.entry_id for item in plan.removed_entries])
        self.assertEqual(40, plan.bytes_after)

    def test_plan_is_deterministic_regardless_of_input_order(self):
        entries = [
            entry(f"e{i}", digest(f"{i % 10}"), created=i % 3, expires=i)
            for i in range(10)
        ]
        first = plan_retention(entries, self.policy(soft=30), now_ms=4)
        second = plan_retention(list(reversed(entries)), self.policy(soft=30), now_ms=4)
        self.assertEqual(first, second)

    def test_rejects_invalid_clock_and_index_invariants(self):
        with self.assertRaises(ValueError):
            plan_retention([], self.policy(), now_ms=-1)
        with self.assertRaisesRegex(RetentionIndexError, "duplicate"):
            plan_retention(
                [entry("a", digest("a")), entry("a", digest("b"))],
                self.policy(),
                now_ms=0,
            )
        with self.assertRaisesRegex(RetentionIndexError, "disagree"):
            plan_retention(
                [entry("a", digest("a"), size=1), entry("b", digest("a"), size=2)],
                self.policy(),
                now_ms=0,
            )
        with self.assertRaisesRegex(RetentionIndexError, "entry limit"):
            plan_retention(
                [entry("a", digest("a")), entry("b", digest("b"))],
                RetentionPolicy(soft_budget_bytes=1, hard_limit_bytes=1, max_entries=1),
                now_ms=0,
            )


class RetentionPolicyAndIndexContractTest(unittest.TestCase):
    def test_policy_validates_limits_and_round_trips(self):
        policy = RetentionPolicy(
            soft_budget_bytes=10,
            hard_limit_bytes=20,
            default_ttl_ms=5,
            ttl_ms_by_type={"frame.raw": None},
        )
        self.assertEqual(
            policy, RetentionPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
        )
        self.assertIsNone(policy.expiry_for("frame.raw", 100))
        self.assertEqual(105, policy.expiry_for("video.segment", 100))
        for kwargs in (
            {"soft_budget_bytes": 30, "hard_limit_bytes": 20},
            {"soft_budget_bytes": 0, "hard_limit_bytes": 20},
            {"soft_budget_bytes": True, "hard_limit_bytes": 20},
            {"soft_budget_bytes": 1, "hard_limit_bytes": 2, "default_ttl_ms": 0},
            {
                "soft_budget_bytes": 1,
                "hard_limit_bytes": 2,
                "ttl_ms_by_type": {"Bad": 1},
            },
            {"soft_budget_bytes": 1, "hard_limit_bytes": 2, "max_entries": 10**9},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(RetentionPolicyError):
                RetentionPolicy(**kwargs)
        with self.assertRaises(RetentionPolicyError):
            RetentionPolicy.from_dict({**policy.to_dict(), "extra": 1})

    def test_schemas_match_python_contracts(self):
        index_schema = json.loads(
            (CONFIG / "retention_index.schema.json").read_text(encoding="utf-8")
        )
        policy_schema = json.loads(
            (CONFIG / "retention_policy.schema.json").read_text(encoding="utf-8")
        )
        entry_schema = index_schema["properties"]["entries"]["items"]
        self.assertEqual(
            set(entry_schema["required"]), set(entry("a", digest("a")).to_dict())
        )
        self.assertEqual(
            set(index_schema["required"]), set(retention_index_document([]))
        )
        policy = RetentionPolicy(soft_budget_bytes=1, hard_limit_bytes=1)
        self.assertEqual(set(policy_schema["required"]), set(policy.to_dict()))

    def test_index_parse_fails_closed_on_corruption(self):
        valid = retention_index_document([entry("a", digest("a"))])
        self.assertEqual(1, len(parse_retention_index(json.dumps(valid).encode())))
        corrupt_payloads = [
            b"{not json",
            b"\xff\xfe",
            b"[]",
            json.dumps({**valid, "schema_version": 2}).encode(),
            json.dumps({**valid, "extra": True}).encode(),
            json.dumps({"schema_version": 1, "entries": {}}).encode(),
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [{**valid["entries"][0], "size_bytes": -1}],
                }
            ).encode(),
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [{**valid["entries"][0], "protected": 1}],
                }
            ).encode(),
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [{**valid["entries"][0], "expires_at_ms": -5}],
                }
            ).encode(),
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [valid["entries"][0], valid["entries"][0]],
                }
            ).encode(),
        ]
        for payload in corrupt_payloads:
            with (
                self.subTest(payload=payload[:40]),
                self.assertRaises(RetentionIndexError),
            ):
                parse_retention_index(payload)
        too_many = json.dumps(
            {"schema_version": 1, "entries": [valid["entries"][0]] * 3}
        ).encode()
        with self.assertRaisesRegex(RetentionIndexError, "entry limit"):
            parse_retention_index(too_many, max_entries=2)


class RetentionCollectorTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.base = Path(self._directory.name)
        self.root = self.base / "artifacts"
        self.store = FileSystemArtifactStore(self.root)
        self.clock = FakeClock()
        self.fs = RecordingFileSystem()

    def tearDown(self):
        self._directory.cleanup()

    def collector(self, **policy) -> RetentionCollector:
        values = {"soft_budget_bytes": 100, "hard_limit_bytes": 200, **policy}
        return RetentionCollector(
            self.store, RetentionPolicy(**values), clock=self.clock, file_system=self.fs
        )

    def put(self, payload: bytes, artifact_type: str = "frame.raw"):
        return self.store.put(
            payload, media_type="application/octet-stream", artifact_type=artifact_type
        )

    def test_gc_enforces_budget_ttl_protected_and_dedup_end_to_end(self):
        collector = self.collector(
            soft_budget_bytes=25, ttl_ms_by_type={"video.segment": 50}
        )
        old = self.put(b"o" * 10)
        protected = self.put(b"p" * 10)
        newest = self.put(b"n" * 10)
        expiring = self.put(b"v" * 5, "video.segment")
        collector.register(old, entry_id="old")
        collector.register(protected, entry_id="keep", protected=True)
        collector.register(old, entry_id="old-dup")
        self.clock.now_ms += 1
        collector.register(newest, entry_id="newest")
        collector.register(expiring, entry_id="clip")
        self.assertTrue(collector.needs_collection())
        self.clock.now_ms += 100

        report = collector.collect()

        reasons = {(item.entry_id, item.reason) for item in report.plan.removed_entries}
        self.assertEqual(
            {("old-dup", "duplicate"), ("clip", "expired"), ("old", "budget")}, reasons
        )
        self.assertEqual(
            ["keep", "newest"], [item.entry_id for item in collector.load_entries()]
        )
        self.assertEqual(
            tuple(sorted((old.sha256, expiring.sha256))), report.deleted_artifacts
        )
        self.assertEqual(20, report.plan.bytes_after)
        self.assertEqual(protected.sha256 and b"p" * 10, self.store.read(protected))
        self.assertEqual((), self.store.check_integrity())
        self.assertFalse(collector.needs_collection())

    def test_gc_never_breaks_live_references(self):
        collector = self.collector(soft_budget_bytes=5, default_ttl_ms=1)
        referenced = self.put(b"r" * 10)
        collector.register(referenced, entry_id="referenced")
        self.clock.now_ms += 10

        self.assertFalse(collector.needs_collection(live_references=[referenced]))
        self.assertTrue(collector.needs_collection())
        report = collector.collect(live_references=[referenced])

        self.assertEqual((), report.deleted_artifacts)
        self.assertEqual(b"r" * 10, self.store.read(referenced))
        self.assertTrue(report.plan.over_budget)

    def test_hard_limit_blocks_admission_and_registration(self):
        collector = self.collector(soft_budget_bytes=10, hard_limit_bytes=15)
        first = self.put(b"a" * 10)
        collector.register(first, entry_id="first", protected=True)
        self.assertFalse(collector.can_admit(10))

        self.assertTrue(collector.can_admit(10, sha256=first.sha256))
        self.assertTrue(collector.can_admit(5))
        oversized = self.put(b"b" * 10)
        with self.assertRaises(RetentionCapacityError):
            collector.register(oversized, entry_id="second")
        self.assertFalse(collector.needs_collection())
        self.assertTrue(collector.plan().over_hard_limit)

    def test_entry_limit_and_duplicate_ids_are_rejected(self):
        collector = self.collector(max_entries=1)
        reference = self.put(b"x")
        collector.register(reference, entry_id="one")
        with self.assertRaisesRegex(RetentionIndexError, "duplicate"):
            collector.register(reference, entry_id="one")
        with self.assertRaises(RetentionCapacityError):
            collector.register(reference, entry_id="two")

    def test_set_protected_saves_an_entry_from_gc(self):
        collector = self.collector(default_ttl_ms=1)
        reference = self.put(b"t" * 3)
        collector.register(reference, entry_id="ttl")
        collector.set_protected("ttl", True)
        self.clock.now_ms += 10
        self.assertEqual((), collector.collect().deleted_artifacts)
        collector.set_protected("ttl", False)
        self.assertEqual((reference.sha256,), collector.collect().deleted_artifacts)

    def test_corrupt_index_fails_closed_without_deleting(self):
        collector = self.collector(soft_budget_bytes=1)
        reference = self.put(b"z" * 10)
        collector.register(reference, entry_id="z")
        index = self.root / RETENTION_INDEX_FILENAME
        index.write_text('{"schema_version": 1, "entries": [', encoding="utf-8")

        with self.assertRaises(RetentionIndexError):
            collector.collect()
        self.assertEqual([], self.fs.removed)
        self.assertEqual(b"z" * 10, self.store.read(reference))

    def test_corrupt_artifact_metadata_or_size_mismatch_fails_closed(self):
        collector = self.collector(soft_budget_bytes=1)
        reference = self.put(b"m" * 10)
        collector.register(reference, entry_id="m")
        metadata_path = self.store.object_path(reference.sha256) / "metadata.json"
        original = metadata_path.read_bytes()
        metadata_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(RetentionIndexError):
            collector.collect()
        metadata_path.write_bytes(original)
        document = json.loads(
            (self.root / RETENTION_INDEX_FILENAME).read_text(encoding="utf-8")
        )
        document["entries"][0]["size_bytes"] = 3
        (self.root / RETENTION_INDEX_FILENAME).write_text(
            json.dumps(document), encoding="utf-8"
        )
        with self.assertRaisesRegex(RetentionIndexError, "size disagrees"):
            collector.collect()
        self.assertEqual([], self.fs.removed)

    def test_corrupt_unindexed_artifact_metadata_fails_closed(self):
        collector = self.collector(soft_budget_bytes=1)
        orphan = self.put(b"q" * 4)
        (self.store.object_path(orphan.sha256) / "metadata.json").write_text(
            "[]", encoding="utf-8"
        )
        with self.assertRaises(RetentionIndexError):
            collector.collect()
        self.assertEqual([], self.fs.removed)

    def test_unindexed_objects_are_counted_but_not_deleted(self):
        collector = self.collector(soft_budget_bytes=5, hard_limit_bytes=20)
        orphan = self.put(b"u" * 8)
        report = collector.collect()
        self.assertEqual((), report.deleted_artifacts)
        self.assertEqual(8, report.plan.bytes_after)
        self.assertTrue(report.plan.over_budget)
        self.assertEqual(b"u" * 8, self.store.read(orphan))

    def test_deletion_outside_managed_root_is_refused(self):
        outside = self.base / "outside" / "aa"
        collector = self.collector(soft_budget_bytes=1)
        reference = self.put(b"e" * 10)
        collector.register(reference, entry_id="e")
        escape = outside / reference.sha256
        escape.mkdir(parents=True)
        (escape / "keep.txt").write_text("must survive", encoding="utf-8")
        self.store.object_path = lambda digest: escape  # type: ignore[method-assign]

        with self.assertRaises(RetentionSafetyError):
            collector.collect()
        self.assertEqual([], self.fs.removed)
        self.assertTrue((escape / "keep.txt").exists())
        self.assertEqual(["e"], [item.entry_id for item in collector.load_entries()])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unsupported")
    def test_symlinked_object_directory_is_refused(self):
        collector = self.collector(soft_budget_bytes=1)
        reference = self.put(b"s" * 10)
        collector.register(reference, entry_id="s")
        object_dir = self.store.object_path(reference.sha256)
        outside = self.base / "victim"
        outside.mkdir()
        (outside / "keep.txt").write_text("must survive", encoding="utf-8")
        real = self.base / "moved"
        os.replace(object_dir, real)
        try:
            os.symlink(outside, object_dir, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation not permitted: {error}")

        with self.assertRaises((RetentionSafetyError, RetentionIndexError)):
            collector.collect()
        self.assertEqual([], self.fs.removed)
        self.assertTrue((outside / "keep.txt").exists())

    def test_junction_fallback_uses_reparse_attribute_without_isjunction(self):
        class FakeStat:
            st_file_attributes = 0x400

        with (
            mock.patch.object(
                retention_collector.os.path, "isjunction", None, create=True
            ),
            mock.patch.object(retention_collector.os, "lstat", return_value=FakeStat()),
        ):
            self.assertTrue(retention_collector._is_junction(self.base))
        with (
            self.assertRaises(RetentionSafetyError),
            mock.patch.object(retention_collector, "_is_junction", return_value=True),
        ):
            LocalRetentionFileSystem().remove_object(self.base)
        self.assertTrue(self.base.exists())

    def test_index_is_rewritten_before_objects_are_deleted(self):
        class FailingRemoval(RecordingFileSystem):
            def remove_object(self, path):
                raise OSError("simulated crash during delete")

        self.fs = FailingRemoval()
        collector = self.collector(soft_budget_bytes=1)
        reference = self.put(b"c" * 10)
        collector.register(reference, entry_id="c")
        with self.assertRaises(OSError):
            collector.collect()
        self.assertEqual((), collector.load_entries())
        self.assertEqual((reference.sha256,), self.store.find_orphans([]))

    def test_invalid_clock_and_live_references_are_rejected(self):
        self.clock.now_ms = -1
        with self.assertRaises(ValueError):
            self.collector().needs_collection()
        self.clock.now_ms = 0
        with self.assertRaises(TypeError):
            self.collector().collect(live_references=["not-a-reference"])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
