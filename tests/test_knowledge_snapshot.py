from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.knowledge_snapshot import (
    KnowledgeSnapshot,
    MAX_KNOWLEDGE_BYTES,
    SNAPSHOT_FIELDS,
)
from ai_game_player.knowledge_snapshot_store import (
    KnowledgeSnapshotStore,
    KNOWLEDGE_MEDIA_TYPE,
)


FIXTURE = Path(__file__).parent / "fixtures/knowledge/snapshot-v1.json"


def fixture():
    return json.loads(FIXTURE.read_text())


class KnowledgeSnapshotTests(unittest.TestCase):
    def test_schema_fields_and_valid_roundtrip_are_consistent(self):
        schema = json.loads(
            (FIXTURE.parents[3] / "config/knowledge_snapshot.schema.json").read_text()
        )
        self.assertEqual(set(schema["required"]), SNAPSHOT_FIELDS)
        self.assertFalse(schema["additionalProperties"])
        value = fixture()
        snapshot = KnowledgeSnapshot.from_dict(value)
        self.assertEqual(snapshot.to_dict(), value)
        self.assertEqual(KnowledgeSnapshot(snapshot.to_bytes()), snapshot)
        self.assertEqual(
            hashlib.sha256(snapshot.to_bytes()).hexdigest(), snapshot.sha256
        )
        self.assertEqual(snapshot.sample_count, 3)
        self.assertEqual(snapshot.uncertainty, 0.7)

    def test_mutable_input_and_returned_copies_cannot_change_snapshot(self):
        value = fixture()
        snapshot = KnowledgeSnapshot.from_dict(value)
        value["candidate_priors"]["score-label"] = 0.9
        copy = snapshot.to_dict()
        copy["scope"]["game_id"] = "other-game"
        self.assertEqual(snapshot.to_dict()["candidate_priors"]["score-label"], -0.4)
        self.assertEqual(snapshot.scope.game_id, "test-game")
        with self.assertRaises(FrozenInstanceError):
            snapshot.version = "other-version"

    def test_schema_rejects_unknown_raw_secret_and_safety_fields(self):
        for name, value in (
            ("raw_frame", "pixels"),
            ("api_key", "secret"),
            ("safety_override", True),
            ("notes", "free text"),
        ):
            payload = fixture()
            payload[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                KnowledgeSnapshot.from_dict(payload)
        for target in ("scope", "builder_state", "weight_hints", "threshold_hints"):
            payload = fixture()
            payload[target]["secret"] = "credential"
            with self.subTest(target=target), self.assertRaises(ValueError):
                KnowledgeSnapshot.from_dict(payload)
        payload = fixture()
        payload["source_experience_range"][0]["raw_frame"] = "pixels"
        with self.assertRaises(ValueError):
            KnowledgeSnapshot.from_dict(payload)
        payload = fixture()
        payload["candidate_priors"]["sk-secret"] = 0.1
        with self.assertRaises(ValueError):
            KnowledgeSnapshot.from_dict(payload)
        payload = fixture()
        payload["created_at"] = "private-value"
        with self.assertRaises(ValueError) as caught:
            KnowledgeSnapshot.from_dict(payload)
        self.assertNotIn("private-value", str(caught.exception))

    def test_invalid_numbers_shapes_versions_ranges_and_keys_are_rejected(self):
        mutations = [
            lambda x: x.update(schema_version=True),
            lambda x: x.update(schema_version=2),
            lambda x: x.update(sample_count=True),
            lambda x: x.update(uncertainty=float("nan")),
            lambda x: x.update(uncertainty=1.01),
            lambda x: x["candidate_priors"].update({"score-label": 2}),
            lambda x: x["candidate_reliability"].update({"score-label": True}),
            lambda x: x["weight_hints"].update({"utility": 2**1024}),
            lambda x: x["action_outcome_stats"]["score-label"].update(count=4),
            lambda x: x["action_outcome_stats"]["score-label"].update(alpha=0),
            lambda x: x["source_experience_range"][0].update(last_sequence=1),
            lambda x: x["source_experience_range"][0].update(event_count=2),
            lambda x: x["source_experience_range"][0].update(sha256="not-a-digest"),
            lambda x: x["roi_hints"].update({"score-panel": [0.9, 0.0, 0.2, 0.1]}),
            lambda x: x["roi_hints"].update({"score-panel": [0.0, 0.0, 0.0, 0.1]}),
            lambda x: x["scope"].update(game_id="../other-game"),
            lambda x: x.update(created_at="2026-11-01T00:00:00"),
            lambda x: x.update(expires_at="2026-10-01T00:00:00Z"),
            lambda x: x["builder_state"]["source_watermarks"].update(
                {"source.one": -1}
            ),
        ]
        for index, mutate in enumerate(mutations):
            payload = fixture()
            mutate(payload)
            with self.subTest(case=index), self.assertRaises((ValueError, TypeError)):
                KnowledgeSnapshot.from_dict(payload)
        with self.assertRaises(ValueError):
            KnowledgeSnapshot(b'{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):
            KnowledgeSnapshot(b" " * (MAX_KNOWLEDGE_BYTES + 1))

    def test_transition_probability_duplicates_and_overlapping_sources_rejected(self):
        value = fixture()
        value["transition_priors"].append(dict(value["transition_priors"][0]))
        with self.assertRaises(ValueError):
            KnowledgeSnapshot.from_dict(value)
        value = fixture()
        other = dict(value["transition_priors"][0])
        other["to_state"] = "other-state"
        value["transition_priors"].append(other)
        with self.assertRaises(ValueError):
            KnowledgeSnapshot.from_dict(value)
        value = fixture()
        value["sample_count"] = 6
        value["source_experience_range"].append(
            dict(value["source_experience_range"][0])
        )
        with self.assertRaises(ValueError):
            KnowledgeSnapshot.from_dict(value)

    def test_store_roundtrip_immutable_versions_and_explicit_expiry(self):
        value = fixture()
        value["expires_at"] = "2026-11-02T00:00:00Z"
        snapshot = KnowledgeSnapshot.from_dict(value)
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            first = store.save(snapshot)
            self.assertEqual(store.save(snapshot), first)
            result = store.read(
                snapshot.scope,
                snapshot.version,
                now=datetime(2026, 11, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(result.reason, "ok")
            self.assertEqual(result.snapshot, snapshot)
            self.assertEqual(result.reference, first)
            self.assertEqual(
                store.read(
                    snapshot.scope,
                    snapshot.version,
                    now=datetime(2026, 11, 2, tzinfo=timezone.utc),
                ).reason,
                "expired",
            )
            changed = fixture()
            changed["candidate_priors"]["score-label"] = 0.2
            with self.assertRaisesRegex(ValueError, "immutable"):
                store.save(KnowledgeSnapshot.from_dict(changed))
            self.assertEqual(len(tuple(store.artifacts._iter_digests())), 1)

    def test_all_scope_dimensions_are_isolated_and_mismatch_falls_back(self):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            store.save(snapshot)
            for field in snapshot.scope.to_dict():
                other = replace(snapshot.scope, **{field: "other"})
                self.assertEqual(store.read(other, snapshot.version).reason, "missing")
                target = store._index_path(other, snapshot.version)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    store._index_path(snapshot.scope, snapshot.version).read_bytes()
                )
                result = store.read(other, snapshot.version)
                self.assertEqual(result.reason, "scope_mismatch")
                self.assertIsNone(result.snapshot)
                self.assertIsNone(result.reference)

    def test_corrupt_truncated_oversized_duplicate_and_missing_index_fall_back(self):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            self.assertEqual(
                store.read(snapshot.scope, snapshot.version).reason, "missing"
            )
            store.save(snapshot)
            path = store._index_path(snapshot.scope, snapshot.version)
            original = path.read_bytes()
            for payload in (
                b"{",
                b"\xff",
                b"{}",
                b" " * 5000,
                b"[" * 1500 + b"]" * 1500,
                b'{"schema_version":1,"schema_version":1}',
            ):
                path.write_bytes(payload)
                result = store.read(snapshot.scope, snapshot.version)
                self.assertEqual(result.reason, "invalid")
                self.assertIsNone(result.snapshot)
            path.write_bytes(original)
            self.assertEqual(store.read(snapshot.scope, snapshot.version).reason, "ok")

    def test_tampered_payload_version_metadata_and_hash_fall_back(self):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            reference = store.save(snapshot)
            payload_path = (
                store.artifacts.root
                / reference.sha256[:2]
                / reference.sha256
                / "payload"
            )
            original = payload_path.read_bytes()
            payload_path.write_bytes(b"truncated")
            self.assertEqual(
                store.read(snapshot.scope, snapshot.version).reason, "invalid"
            )
            payload_path.write_bytes(original)
            metadata_path = payload_path.with_name("metadata.json")
            metadata = metadata_path.read_bytes()
            metadata_path.write_bytes(b" " * 20000)
            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("unbounded read")
            ):
                self.assertEqual(
                    store.read(snapshot.scope, snapshot.version).reason, "invalid"
                )
            metadata_path.write_bytes(metadata)
            payload_path.write_bytes(original + b" " * MAX_KNOWLEDGE_BYTES)
            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("unbounded read")
            ):
                self.assertEqual(
                    store.read(snapshot.scope, snapshot.version).reason, "invalid"
                )
            payload_path.write_bytes(original)
            index_path = store._index_path(snapshot.scope, snapshot.version)
            index = json.loads(index_path.read_text())
            changed = fixture()
            changed["schema_version"] = 2
            wrong = store.artifacts.put(
                json.dumps(changed).encode(),
                media_type=KNOWLEDGE_MEDIA_TYPE,
                artifact_type="knowledge.snapshot",
            )
            index["reference"] = wrong.to_dict()
            index_path.write_text(json.dumps(index))
            self.assertEqual(
                store.read(snapshot.scope, snapshot.version).reason, "invalid"
            )

    def test_concurrent_same_version_is_idempotent_and_failure_never_publishes_index(
        self,
    ):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            with ThreadPoolExecutor(max_workers=6) as pool:
                refs = list(pool.map(lambda _: store.save(snapshot), range(12)))
            self.assertEqual(len(set(refs)), 1)
            self.assertEqual(
                store.read(snapshot.scope, snapshot.version).snapshot, snapshot
            )
            value = fixture()
            value["version"] = "2"
            newer = KnowledgeSnapshot.from_dict(value)
            with patch(
                "ai_game_player.knowledge_snapshot_store.stage_json_write",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaises(OSError):
                    store.save(newer)
            self.assertEqual(store.read(snapshot.scope, "2").reason, "missing")
            self.assertEqual(
                store.read(snapshot.scope, snapshot.version).snapshot, snapshot
            )

    def test_bounded_artifact_api_preserves_integrity_and_rejects_invalid_limits(self):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            ref = store.save(snapshot)
            self.assertEqual(
                store.artifacts.read_bounded(ref, max_bytes=MAX_KNOWLEDGE_BYTES),
                snapshot.to_bytes(),
            )
            for limit in (0, -1, True, 1.0):
                with self.assertRaises(ValueError):
                    store.artifacts.read_bounded(ref, max_bytes=limit)
            with self.assertRaisesRegex(ValueError, "exceeds read limit"):
                store.artifacts.read_bounded(ref, max_bytes=10)


if __name__ == "__main__":
    unittest.main()
