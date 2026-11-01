from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.knowledge_snapshot import (
    KnowledgeSnapshot,
    MAX_KNOWLEDGE_BYTES,
    SNAPSHOT_FIELDS,
)
from ai_game_player.artifact_store import (
    ArtifactStore,
    BoundedArtifactStore,
    FileSystemArtifactStore,
    MAX_BOUNDED_METADATA_BYTES,
)
from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.bounded_file import read_regular_file
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
        value["transition_priors"][0]["probability"] = 0.25
        value["transition_priors"].append(dict(value["transition_priors"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
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

    def test_processed_cursors_cover_accepted_evidence_without_rejecting_correlated_drops(
        self,
    ):
        for watermark in (None, 0, 2):
            value = fixture()
            if watermark is None:
                value["builder_state"]["source_watermarks"].clear()
            else:
                value["builder_state"]["source_watermarks"]["session.one"] = watermark
            with self.subTest(watermark=watermark), self.assertRaises(ValueError):
                KnowledgeSnapshot.from_dict(value)
        value = fixture()
        value["builder_state"]["source_watermarks"]["session.one"] = 5
        value["builder_state"]["source_watermarks"]["correlated-source"] = 1
        restored = KnowledgeSnapshot.from_dict(value).to_dict()
        self.assertEqual(restored["source_experience_range"][0]["last_sequence"], 3)
        self.assertEqual(
            restored["builder_state"]["source_watermarks"]["session.one"], 5
        )

    def test_wrong_artifact_metadata_type_is_not_usable_knowledge(self):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            reference = store.save(snapshot)
            path = (
                store.artifacts.root
                / reference.sha256[:2]
                / reference.sha256
                / "metadata.json"
            )
            metadata = json.loads(path.read_text())
            metadata["artifact_type"] = "model.adapter"
            path.write_text(json.dumps(metadata))
            result = store.read(snapshot.scope, snapshot.version)
            self.assertEqual((result.snapshot, result.reason), (None, "invalid"))

    def test_shape_and_string_limits_are_checked_before_any_json_encoding(self):
        for mutate in (
            lambda value: value["scope"].update(
                game_id="a" * (4 * MAX_KNOWLEDGE_BYTES)
            ),
            lambda value: value.update(raw_frame="x" * (4 * MAX_KNOWLEDGE_BYTES)),
        ):
            value = fixture()
            mutate(value)
            with patch.object(
                json.JSONEncoder,
                "iterencode",
                side_effect=AssertionError("encoded an invalid giant value"),
            ):
                with self.assertRaises(ValueError):
                    KnowledgeSnapshot.from_dict(value)
        with (
            patch("ai_game_player.knowledge_snapshot.MAX_KNOWLEDGE_BYTES", 128),
            patch.object(json, "dumps", side_effect=AssertionError("unbounded dumps")),
        ):
            with self.assertRaises(ValueError):
                KnowledgeSnapshot.from_dict(fixture())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixture is POSIX-only")
    def test_fifo_index_and_opened_non_regular_descriptor_fail_without_stream_read(
        self,
    ):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            store.save(snapshot)
            path = store._index_path(snapshot.scope, snapshot.version)
            path.unlink()
            os.mkfifo(path)
            with (
                patch.object(
                    Path,
                    "open",
                    side_effect=AssertionError("blocking Path.open on FIFO"),
                ),
                patch(
                    "ai_game_player.bounded_file.os.open",
                    side_effect=AssertionError("must reject existing FIFO before open"),
                ),
            ):
                self.assertEqual(
                    store.read(snapshot.scope, snapshot.version).reason, "invalid"
                )
            path.unlink()
            path.write_bytes(b"{}")
            with (
                patch(
                    "ai_game_player.bounded_file.os.fstat",
                    return_value=SimpleNamespace(st_mode=0o010000, st_size=0),
                ),
                patch(
                    "ai_game_player.bounded_file.os.fdopen",
                    side_effect=AssertionError("read a non-regular FD"),
                ),
            ):
                with self.assertRaises(ValueError):
                    read_regular_file(path, max_bytes=4096)

    def test_bounded_metadata_stream_reads_are_observed_not_only_invalid_json_result(
        self,
    ):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            reference = store.save(snapshot)
            path = (
                store.artifacts.root
                / reference.sha256[:2]
                / reference.sha256
                / "metadata.json"
            )
            original = path.read_bytes()
            sizes = []
            real_fdopen = os.fdopen

            class ObservedStream:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def read(self, size=-1):
                    sizes.append(size)
                    if size < 0:
                        raise AssertionError("unbounded stream read")
                    return self.stream.read(size)

            def observed_fdopen(*args, **kwargs):
                return ObservedStream(real_fdopen(*args, **kwargs))

            with patch(
                "ai_game_player.bounded_file.os.fdopen", side_effect=observed_fdopen
            ):
                self.assertEqual(
                    store.read(snapshot.scope, snapshot.version).reason, "ok"
                )
            self.assertEqual(
                sizes, [4097, MAX_BOUNDED_METADATA_BYTES + 1, MAX_KNOWLEDGE_BYTES + 1]
            )
            sizes.clear()
            path.write_bytes(b" " * 1024)
            with patch(
                "ai_game_player.bounded_file.os.fdopen", side_effect=observed_fdopen
            ):
                self.assertEqual(
                    store.read(snapshot.scope, snapshot.version).reason, "invalid"
                )
            self.assertEqual(sizes, [4097, MAX_BOUNDED_METADATA_BYTES + 1])
            path.write_bytes(original)

    def test_schema_patterns_match_utc_identifiers_and_document_cross_field_rules(self):
        schema = json.loads(
            (FIXTURE.parents[3] / "config/knowledge_snapshot.schema.json").read_text()
        )
        time_pattern = schema["properties"]["created_at"]["pattern"]
        for timestamp, accepted in (
            ("2026-11-01T00:00:00Z", True),
            ("2026-11-01T00:00:00.123456+00:00", True),
            ("2026-11-01T09:00:00+09:00", False),
            ("2026-11-01 00:00:00Z", False),
            ("2026-11-01T00:00:00", False),
        ):
            self.assertEqual(
                re.fullmatch(time_pattern, timestamp) is not None, accepted
            )
            value = fixture()
            value["created_at"] = timestamp
            if accepted:
                KnowledgeSnapshot.from_dict(value)
            else:
                with self.assertRaises(ValueError):
                    KnowledgeSnapshot.from_dict(value)
        identifier_pattern = schema["properties"]["candidate_priors"]["propertyNames"][
            "pattern"
        ]
        for name in ("sk-example", "ghp_example", "github_pat_example"):
            self.assertIsNone(re.fullmatch(identifier_pattern, name))
        roi = schema["properties"]["roi_hints"]["additionalProperties"]
        self.assertEqual(roi["prefixItems"][2]["exclusiveMinimum"], 0)
        self.assertEqual(roi["x-runtime-constraints"], ["x+width<=1", "y+height<=1"])

    def test_bounded_capability_does_not_break_legacy_artifact_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemArtifactStore(Path(directory))
            legacy = SimpleNamespace(
                put=store.put,
                read=store.read,
                verify=store.verify,
                find_orphans=store.find_orphans,
                check_integrity=store.check_integrity,
            )
            self.assertIsInstance(legacy, ArtifactStore)
            self.assertNotIsInstance(legacy, BoundedArtifactStore)
            self.assertIsInstance(store, BoundedArtifactStore)

    def test_missing_knowledge_uses_actual_static_baseline_decision(self):
        observation = ScreenObservation(
            "game", 100, 80, features={"signature": "frame.one"}
        )
        candidates = [
            ActionCandidate("a", "wait", "A", confidence=0.6),
            ActionCandidate("b", "wait", "B", confidence=0.95),
        ]
        context = DecisionContextBuilder().build(observation, candidates, candidates)
        expected = BaselineDecisionProvider().decide(context)
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            loaded = KnowledgeSnapshotStore(Path(directory)).read(
                snapshot.scope, snapshot.version
            )
            provider = (
                BaselineDecisionProvider()
                if loaded.snapshot is None
                else BaselineDecisionProvider(
                    knowledge_snapshot=loaded.snapshot.to_dict()
                )
            )
            actual = provider.decide(context)
            self.assertEqual(actual.selected_action_id, expected.selected_action_id)
            self.assertEqual(actual.rule_id, expected.rule_id)
            self.assertIsNone(actual.knowledge_snapshot_version)

    def test_snapshot_index_directory_is_synced_after_new_and_idempotent_save(self):
        snapshot = KnowledgeSnapshot.from_dict(fixture())
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeSnapshotStore(Path(directory))
            expected_parent = store._index_path(snapshot.scope, snapshot.version).parent
            original = FileSystemArtifactStore._fsync_directory
            with patch.object(
                FileSystemArtifactStore, "_fsync_directory", wraps=original
            ) as sync:
                store.save(snapshot)
                self.assertEqual(sync.call_args.args, (expected_parent,))
                store.save(snapshot)
                self.assertEqual(sync.call_args.args, (expected_parent,))


if __name__ == "__main__":
    unittest.main()
