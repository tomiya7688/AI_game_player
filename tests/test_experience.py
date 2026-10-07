import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from ai_game_player.storage.experience import (
    EXPERIENCE_SCHEMA_VERSION,
    ArtifactReference,
    ExperienceEpisode,
    ExperienceEvent,
    ExperienceProvenance,
    ExperienceStep,
    InMemoryExperienceReader,
)
from ai_game_player.storage.artifact_store import FileSystemArtifactStore
from ai_game_player.storage.experience_reader import ExperienceRecoveryError, JsonlExperienceReader, migrate_experience_dict


def episode() -> ExperienceEpisode:
    reference = ArtifactReference("frame.initial", "a" * 64, "image/png", True)
    provenance = ExperienceProvenance(
        source="human_correction", actor_id="user.local", model_id="ocr.engine",
        model_version="2.0.1", confidence=0.95, evidence_event_id="event.observation",
    )
    event = ExperienceEvent(
        event_id="event.observation", event_type="screen.observed", timestamp="2026-09-30T10:00:00Z",
        source="capture", payload={"width": 640, "height": 480}, model_id="ocr.engine",
        model_version="2.0.1", artifacts=(reference,), provenance=provenance,
    )
    step = ExperienceStep(
        step_id="step.0001", sequence=0, timestamp="2026-09-30T10:00:00+00:00", source="runtime",
        events=(event,), artifacts=(reference,), provenance=provenance,
    )
    return ExperienceEpisode(
        episode_id="episode.sample", created_at="2026-09-30T10:00:00Z", source="game.session",
        model_id="decision.local", model_version="1.1.0", artifacts=(reference,),
        provenance=ExperienceProvenance(source="user_session", actor_id="user.local"), steps=(step,),
    )


class ExperienceSchemaTest(unittest.TestCase):
    def test_episode_round_trips_and_tracks_nested_event_provenance_and_artifacts(self):
        original = episode()
        restored = ExperienceEpisode.from_dict(original.to_dict())

        self.assertEqual(original, restored)
        self.assertEqual(EXPERIENCE_SCHEMA_VERSION, restored.schema_version)
        self.assertEqual("ocr.engine", restored.steps[0].events[0].provenance.model_id)
        self.assertTrue(restored.steps[0].events[0].artifacts[0].sensitive)

    def test_canonical_schema_is_valid_json(self):
        path = Path(__file__).parents[1] / "config" / "experience.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("object", schema["type"])
        self.assertIn("steps", schema["properties"])
        self.assertIn("artifactReference", schema["$defs"])

    def test_rejects_unknown_fields_and_unsupported_schema_version(self):
        value = episode().to_dict()
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            ExperienceEpisode.from_dict(dict(value, unexpected="value"))
        with self.assertRaisesRegex(ValueError, "unsupported experience schema"):
            ExperienceEpisode.from_dict(dict(value, schema_version=EXPERIENCE_SCHEMA_VERSION + 1))

    def test_requires_timezone_and_model_version_pair(self):
        with self.assertRaisesRegex(ValueError, "include a timezone"):
            ExperienceEvent("event.bad", "action.sent", "2026-09-30T10:00:00", "runtime")
        with self.assertRaisesRegex(ValueError, "declared together"):
            ExperienceEpisode("episode.bad", "2026-09-30T10:00:00Z", "runtime", model_id="model.one")

    def test_episode_enforces_unique_step_sequence_and_event_ids(self):
        first = ExperienceStep("step.one", 0, "2026-09-30T10:00:00Z", "runtime")
        duplicate_step = ExperienceStep("step.two", 0, "2026-09-30T10:00:01Z", "runtime")
        with self.assertRaisesRegex(ValueError, "sequence numbers must be unique"):
            ExperienceEpisode("episode.dup", "2026-09-30T10:00:00Z", "runtime", steps=(first, duplicate_step))

        event = ExperienceEvent("event.same", "action.sent", "2026-09-30T10:00:00Z", "runtime")
        left = ExperienceStep("step.left", 0, "2026-09-30T10:00:00Z", "runtime", events=(event,))
        right = ExperienceStep("step.right", 1, "2026-09-30T10:00:01Z", "runtime", events=(event,))
        with self.assertRaisesRegex(ValueError, "event IDs must be unique within an episode"):
            ExperienceEpisode("episode.dup-events", "2026-09-30T10:00:00Z", "runtime", steps=(left, right))

    def test_artifact_reference_requires_digest_mime_and_sensitivity_flag(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            ArtifactReference("artifact.bad", "not-a-hash", "image/png", False)
        with self.assertRaisesRegex(ValueError, "sensitive must be a boolean"):
            ArtifactReference("artifact.bad", "b" * 64, "image/png", "false")

    def test_reader_exposes_logical_episode_api_and_rejects_duplicate_ids(self):
        record = episode()
        reader = InMemoryExperienceReader((record,))
        self.assertEqual(record, reader.get_episode(record.episode_id))
        self.assertEqual((record,), tuple(reader.iter_episodes()))
        self.assertIsNone(reader.get_episode("episode.missing"))
        with self.assertRaisesRegex(ValueError, "duplicate episode ID"):
            InMemoryExperienceReader((record, record))


class ExperienceReaderTest(unittest.TestCase):
    def _episode_with_states(self) -> ExperienceEpisode:
        snapshot = ExperienceEvent(
            "event.snapshot", "state.snapshot", "2026-09-30T10:00:00Z", "runtime",
            payload={"state": {"hp": 10, "room": "start"}},
        )
        delta = ExperienceEvent(
            "event.delta", "state.delta", "2026-09-30T10:00:01Z", "runtime",
            payload={"set": {"hp": 8, "room": "next"}, "remove": []},
        )
        first = ExperienceStep("step.initial", 0, "2026-09-30T10:00:00Z", "runtime", events=(snapshot,))
        second = ExperienceStep("step.next", 1, "2026-09-30T10:00:01Z", "runtime", events=(delta,))
        return ExperienceEpisode("episode.states", "2026-09-30T10:00:00Z", "game.session", steps=(first, second))

    def test_reads_arbitrary_step_and_reconstructs_snapshot_plus_deltas(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            path = Path(directory) / "episodes.jsonl"
            path.write_text(json.dumps(self._episode_with_states().to_dict()) + "\n", encoding="utf-8")
            reader = JsonlExperienceReader(path)

            self.assertEqual("step.next", reader.get_step("episode.states", "step.next").step_id)
            self.assertEqual({"hp": 8, "room": "next"}, reader.reconstruct_state("episode.states", "step.next"))
            self.assertEqual({"hp": 10, "room": "start"}, reader.reconstruct_state("episode.states", "step.initial"))
            self.assertIsNone(reader.get_step("episode.states", "step.missing"))

    def test_query_filters_indexed_episodes(self):
        first = self._episode_with_states()
        other = ExperienceEpisode("episode.other", "2026-09-30T11:00:00Z", "other.source")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            path = Path(directory) / "episodes.jsonl"
            path.write_text("\n".join(json.dumps(item.to_dict()) for item in (first, other)) + "\n", encoding="utf-8")
            reader = JsonlExperienceReader(path)
            self.assertEqual((first,), reader.find(source="game.session", created_after="2026-09-30T09:00:00Z"))

    def test_migrates_legacy_v0_episode_step_and_event(self):
        legacy = self._episode_with_states().to_dict()
        legacy.pop("schema_version")
        for field in ("model_id", "model_version", "artifacts", "provenance"):
            legacy.pop(field)
        for step in legacy["steps"]:
            for field in ("model_id", "model_version", "artifacts", "provenance"):
                step.pop(field)
            for event in step["events"]:
                for field in ("artifacts", "provenance", "model_id", "model_version"):
                    event.pop(field)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            path = Path(directory) / "legacy.jsonl"
            path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
            reader = JsonlExperienceReader(path)
            restored = reader.get_episode("episode.states")
            self.assertEqual(EXPERIENCE_SCHEMA_VERSION, restored.schema_version)
            self.assertEqual({"hp": 8, "room": "next"}, reader.reconstruct_state("episode.states", "step.next"))
            self.assertEqual(EXPERIENCE_SCHEMA_VERSION, migrate_experience_dict(legacy)["schema_version"])

    def test_recovers_truncated_final_record_but_rejects_corrupt_interior(self):
        valid = json.dumps(self._episode_with_states().to_dict()) + "\n"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            path = Path(directory) / "episodes.jsonl"
            path.write_text(valid + '{"schema_version":', encoding="utf-8")
            reader = JsonlExperienceReader(path)
            self.assertEqual(("episode.states",), tuple(item.episode_id for item in reader.iter_episodes()))
            self.assertEqual(1, len(reader.recovery_notices))

            path.write_text('{bad json}\n' + valid, encoding="utf-8")
            with self.assertRaisesRegex(ExperienceRecoveryError, "interior record"):
                JsonlExperienceReader(path)

            unsupported = self._episode_with_states().to_dict()
            unsupported["schema_version"] = EXPERIENCE_SCHEMA_VERSION + 1
            path.write_text(json.dumps(unsupported), encoding="utf-8")
            with self.assertRaisesRegex(ExperienceRecoveryError, "unsupported experience schema"):
                JsonlExperienceReader(path)

    def test_validates_checkpoint_and_artifact_references(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            root = Path(directory)
            store = FileSystemArtifactStore(root / "artifacts")
            reference = store.put(b"frame", media_type="image/png", sensitive=True)
            record = ExperienceEpisode(
                "episode.with-artifact", "2026-09-30T10:00:00Z", "game.session", artifacts=(reference,),
            )
            path = root / "episodes.jsonl"
            raw = (json.dumps(record.to_dict()) + "\n").encode("utf-8")
            path.write_bytes(raw)
            checkpoint = {"line_count": 1, "sha256": hashlib.sha256(raw).hexdigest()}
            Path(str(path) + ".checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
            reader = JsonlExperienceReader(path, artifact_store=store)
            self.assertTrue(reader.checkpoint_valid)

            missing = ArtifactReference("artifact.missing", "0" * 64, "image/png", True)
            broken = ExperienceEpisode(
                "episode.missing-artifact", "2026-09-30T10:00:00Z", "game.session", artifacts=(missing,),
            )
            path.write_text(json.dumps(broken.to_dict()) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact not found"):
                JsonlExperienceReader(path, artifact_store=store)

            path.write_bytes(raw)
            checkpoint["sha256"] = "0" * 64
            Path(str(path) + ".checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
            reader = JsonlExperienceReader(path, artifact_store=store)
            self.assertFalse(reader.checkpoint_valid)
            self.assertEqual(1, len(reader.recovery_notices))


if __name__ == "__main__":
    unittest.main()
