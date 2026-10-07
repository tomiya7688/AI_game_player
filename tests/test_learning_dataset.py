import hashlib
import json
import unittest
from pathlib import Path

from ai_game_player.storage.experience import (
    ArtifactReference,
    ExperienceEpisode,
    ExperienceEvent,
    ExperienceProvenance,
    ExperienceStep,
    InMemoryExperienceReader,
)
from ai_game_player.learning.contracts import LearningCapabilityManifest
from ai_game_player.learning.contracts.dataset import ExperienceDatasetBuilder, ExperienceTrainingDataset


def manifest(**kwargs) -> LearningCapabilityManifest:
    values = {
        "model_id": "decision.local",
        "model_version": "1.2.0",
        "trainable": True,
        "update_strategies": frozenset({"correction"}),
        "dataset_schema_id": "training.samples",
        "dataset_schema_version": "1.0",
        "trainer_adapter_id": "trainer.local",
        "trainer_adapter_version": "1.0",
        "required_runtime": frozenset({"cpu"}),
        "output_artifact_type": "model.delta",
        "evaluation_suite": "regression.basic",
        "promotion_policy": "no_regression",
        "rollback_source": "champion.pointer",
    }
    values.update(kwargs)
    return LearningCapabilityManifest(**values)


def sample_event(event_id: str, event_type: str, payload: dict, provenance: ExperienceProvenance | None = None,
                 artifacts: tuple[ArtifactReference, ...] = ()) -> ExperienceEvent:
    return ExperienceEvent(
        event_id=event_id,
        event_type=event_type,
        timestamp="2026-10-01T10:00:00Z",
        source="experience.runtime",
        payload=payload,
        model_id="decision.local",
        model_version="1.2.0",
        provenance=provenance,
        artifacts=artifacts,
    )


def episode(episode_id: str, events: tuple[ExperienceEvent, ...]) -> ExperienceEpisode:
    step = ExperienceStep("step.0001", 0, "2026-10-01T10:00:00Z", "experience.runtime", events=events)
    return ExperienceEpisode(episode_id, "2026-10-01T10:00:00Z", "game.session", steps=(step,))


class ExperienceDatasetBuilderTest(unittest.TestCase):
    def setUp(self):
        human = ExperienceProvenance(source="human_correction", actor_id="user.local", confidence=1.0)
        automatic = ExperienceProvenance(source="automatic_label", model_id="labeler.local", model_version="1.0", confidence=0.6)
        outcome = ExperienceProvenance(source="outcome_evidence", confidence=0.9)
        sensitive = ArtifactReference("artifact.frame", "a" * 64, "image/png", True)
        self.events = (
            sample_event("event.decision", "decision.sample", {"input": {"instruction": "choose"}, "target": {"action": "move.left"}}, automatic),
            sample_event("event.ocr", "ocr.correction", {"input": "l0", "target": "10", "verified": True}, human),
            sample_event("event.ui", "ui.region.label", {"input": {"box": [1, 2, 3, 4]}, "target": "button"}, human, (sensitive,)),
            sample_event("event.embedding", "embedding.example", {"input": "icon.a", "target": "icon.b", "relation": "positive"}, automatic),
            sample_event("event.evaluator", "evaluator.sample", {"input": {"expected": "progress"}, "target": 0.8}, outcome),
        )
        self.builder = ExperienceDatasetBuilder(manifest())

    def test_builds_all_typed_samples_with_label_provenance_and_privacy_metadata(self):
        reader = InMemoryExperienceReader((episode("episode.sample", self.events),))
        result = self.builder.build(reader, split_seed=17)

        self.assertEqual(
            {"decision.instruction_response", "ocr.correction", "ui.region_class", "embedding.example", "evaluator.calibration"},
            {sample.sample_type for sample in result.samples},
        )
        by_type = {sample.sample_type: sample for sample in result.samples}
        self.assertEqual("automatic_label", by_type["decision.instruction_response"].label_source)
        self.assertFalse(by_type["decision.instruction_response"].verified)
        self.assertEqual("human_correction", by_type["ocr.correction"].label_source)
        self.assertTrue(by_type["ocr.correction"].verified)
        self.assertEqual("outcome_evidence", by_type["evaluator.calibration"].label_source)
        self.assertFalse(by_type["ui.region_class"].artifacts)
        self.assertTrue(by_type["ui.region_class"].sensitive_artifacts_excluded)
        self.assertEqual("decision.local", result.model_id)
        self.assertEqual("training.samples", result.schema_id)
        self.assertTrue(all(sample.sources for sample in result.samples))

    def test_split_is_reproducible_and_dedup_merges_source_references(self):
        first = episode("episode.one", (self.events[0],))
        duplicate = sample_event(
            "event.decision.copy", "decision.sample", self.events[0].payload,
            self.events[0].provenance,
        )
        second = episode("episode.two", (duplicate,))
        result = self.builder.build((first, second), split_seed=7, validation_fraction=0.35)
        replay = self.builder.build((second, first), split_seed=7, validation_fraction=0.35)

        self.assertEqual(result, replay)
        self.assertEqual(1, len(result.samples))
        self.assertEqual(2, len(result.samples[0].sources))
        self.assertEqual("train" if result.samples[0].split == "train" else "validation", result.samples[0].split)

    def test_split_assigns_train_and_validation_reproducibly_for_multiple_samples(self):
        events = tuple(
            sample_event(
                f"event.sample.{index:03d}", "decision.sample",
                {"input": {"instruction": f"state-{index}"}, "target": {"action": f"move-{index}"}},
            )
            for index in range(64)
        )
        source = episode("episode.batch", events)
        first = self.builder.build((source,), split_seed=9, validation_fraction=0.25)
        same = self.builder.build((source,), split_seed=9, validation_fraction=0.25)
        different_seed = self.builder.build((source,), split_seed=10, validation_fraction=0.25)

        self.assertEqual(first, same)
        self.assertEqual({"train", "validation"}, {sample.split for sample in first.samples})
        self.assertNotEqual(
            tuple(sample.split for sample in first.samples),
            tuple(sample.split for sample in different_seed.samples),
        )

    def test_sensitive_artifacts_are_included_only_by_explicit_opt_in(self):
        record = episode("episode.sensitive", (self.events[2],))
        omitted = self.builder.build((record,))
        included = self.builder.build((record,), include_sensitive_artifacts=True)

        self.assertEqual((), omitted.samples[0].artifacts)
        self.assertTrue(omitted.samples[0].sensitive_artifacts_excluded)
        self.assertTrue(included.samples[0].artifacts[0].sensitive)
        self.assertFalse(included.samples[0].sensitive_artifacts_excluded)

    def test_dataset_round_trips_schema_and_detects_content_tampering(self):
        result = self.builder.build((episode("episode.sample", self.events),), split_seed=29)
        restored = ExperienceTrainingDataset.from_json(result.to_json())
        schema_path = Path(__file__).parents[1] / "config" / "learning_dataset.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(result, restored)
        self.assertEqual("object", schema["type"])
        self.assertIn("digest", schema["required"])
        encoded = result.to_dict()
        encoded["samples"][0]["target"] = "tampered"
        sample = encoded["samples"][0]
        fingerprint_fields = {
            "sample_type", "model_id", "model_version", "input", "target", "relation", "label_source",
            "label_confidence", "verified", "provenance", "artifacts", "sensitive_artifacts_excluded",
        }
        sample_content = {key: sample[key] for key in fingerprint_fields}
        canonical = json.dumps(sample_content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sample["sample_id"] = f"sample.{hashlib.sha256(canonical).hexdigest()}"
        with self.assertRaisesRegex(ValueError, "dataset digest does not match content"):
            ExperienceTrainingDataset.from_dict(encoded)

    def test_automatic_labels_cannot_be_marked_verified_and_untrainable_models_are_rejected(self):
        event = sample_event(
            "event.bad-label", "ocr.correction",
            {"input": "old", "target": "new", "label_source": "automatic_label", "verified": True},
        )
        with self.assertRaisesRegex(ValueError, "only a human correction"):
            self.builder.build((episode("episode.bad-label", (event,)),))

        with self.assertRaisesRegex(ValueError, "not trainable"):
            ExperienceDatasetBuilder(manifest(
                trainable=False, update_strategies=frozenset(), dataset_schema_id=None,
                dataset_schema_version=None, trainer_adapter_id=None, trainer_adapter_version=None,
                output_artifact_type=None, evaluation_suite=None, promotion_policy=None, rollback_source=None,
                untrainable_reason="no adapter",
            ))


if __name__ == "__main__":
    unittest.main()
