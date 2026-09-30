import json
import unittest
from pathlib import Path

from ai_game_player.experience import (
    EXPERIENCE_SCHEMA_VERSION,
    ArtifactReference,
    ExperienceEpisode,
    ExperienceEvent,
    ExperienceProvenance,
    ExperienceStep,
    InMemoryExperienceReader,
)


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


if __name__ == "__main__":
    unittest.main()
