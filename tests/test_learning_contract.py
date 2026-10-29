import json
import unittest
from pathlib import Path

from ai_game_player.learning import (
    LearningArtifactProvenance,
    LearningCapabilityManifest,
    LearningCapabilityRegistry,
)


def trainable_manifest(**kwargs) -> LearningCapabilityManifest:
    values = {
        "model_id": "ocr.sample",
        "model_version": "1.2.0",
        "trainable": True,
        "update_strategies": frozenset({"corrected_samples"}),
        "dataset_schema_id": "ocr.corrections",
        "dataset_schema_version": "1.0",
        "trainer_adapter_id": "ocr.trainer",
        "trainer_adapter_version": "2.1",
        "required_runtime": frozenset({"cpu"}),
        "output_artifact_type": "ocr.model_delta",
        "evaluation_suite": "ocr.regression",
        "promotion_policy": "no_regression",
        "rollback_source": "champion_pointer",
    }
    values.update(kwargs)
    return LearningCapabilityManifest(**values)


class LearningContractTest(unittest.TestCase):
    def test_manifest_round_trips_machine_readable_contract(self):
        original = trainable_manifest()
        self.assertEqual(original, LearningCapabilityManifest.from_dict(original.to_dict()))

    def test_contract_schema_is_parseable(self):
        config = Path(__file__).parents[1] / "config"
        schema = json.loads((config / "learning_capability.schema.json").read_text(encoding="utf-8"))
        artifact_schema = json.loads((config / "learning_artifact_provenance.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("object", schema["type"])
        self.assertIn("trainer_adapter_version", schema["required"])
        self.assertEqual(2, len(schema["oneOf"]))
        self.assertIn("source_dataset_digest", artifact_schema["required"])

    def test_registry_resolves_model_profile_and_exposes_untrainable_reason(self):
        registry = LearningCapabilityRegistry()
        registry.register(trainable_manifest())
        registry.register(
            LearningCapabilityManifest(
                model_id="vision.legacy",
                model_version="3",
                trainable=False,
                update_strategies=frozenset(),
                dataset_schema_id=None,
                dataset_schema_version=None,
                trainer_adapter_id=None,
                trainer_adapter_version=None,
                required_runtime=frozenset(),
                output_artifact_type=None,
                evaluation_suite=None,
                promotion_policy=None,
                rollback_source=None,
                untrainable_reason="trainer adapter is not implemented",
            )
        )

        self.assertTrue(registry.resolve("ocr.sample").trainable)
        legacy = registry.resolve("vision.legacy")
        self.assertFalse(legacy.trainable)
        self.assertEqual("trainer adapter is not implemented", legacy.untrainable_reason)
        with self.assertRaisesRegex(ValueError, "not trainable"):
            legacy.validate_compatibility(
                dataset_schema_id="x", dataset_schema_version="1", trainer_adapter_id="x",
                trainer_adapter_version="1", output_artifact_type="x",
            )

    def test_rejects_incompatible_dataset_trainer_artifact_and_runtime(self):
        contract = trainable_manifest()
        with self.assertRaisesRegex(ValueError, "dataset schema version"):
            contract.validate_compatibility(
                dataset_schema_id="ocr.corrections", dataset_schema_version="2.0",
                trainer_adapter_id="ocr.trainer", trainer_adapter_version="2.1",
                output_artifact_type="ocr.model_delta",
            )
        with self.assertRaisesRegex(ValueError, "trainer adapter version"):
            contract.validate_compatibility(
                dataset_schema_id="ocr.corrections", dataset_schema_version="1.0",
                trainer_adapter_id="ocr.trainer", trainer_adapter_version="3.0",
                output_artifact_type="ocr.model_delta",
            )
        with self.assertRaisesRegex(ValueError, "output artifact type"):
            contract.validate_compatibility(
                dataset_schema_id="ocr.corrections", dataset_schema_version="1.0",
                trainer_adapter_id="ocr.trainer", trainer_adapter_version="2.1",
                output_artifact_type="wrong.type",
            )
        with self.assertRaisesRegex(ValueError, "missing runtime requirements: cpu"):
            contract.validate_compatibility(
                dataset_schema_id="ocr.corrections", dataset_schema_version="1.0",
                trainer_adapter_id="ocr.trainer", trainer_adapter_version="2.1",
                output_artifact_type="ocr.model_delta", available_runtime=set(),
            )

    def test_rejects_missing_learning_strategy_or_untrainable_reason(self):
        with self.assertRaisesRegex(ValueError, "at least one update strategy"):
            trainable_manifest(update_strategies=frozenset())
        with self.assertRaisesRegex(ValueError, "untrainable_reason"):
            LearningCapabilityManifest(
                model_id="legacy.model", model_version="1", trainable=False,
                update_strategies=frozenset(), dataset_schema_id=None, dataset_schema_version=None,
                trainer_adapter_id=None, trainer_adapter_version=None, required_runtime=frozenset(),
                output_artifact_type=None, evaluation_suite=None, promotion_policy=None,
                rollback_source=None,
            )

    def test_artifact_provenance_is_versioned_and_tied_to_model_contract(self):
        artifact = LearningArtifactProvenance(
            artifact_id="artifact.delta.01", model_id="ocr.sample", base_model_version="1.2.0",
            artifact_version="1.2.1", artifact_type="ocr.model_delta", source_dataset_id="dataset.run.01",
            source_dataset_schema_id="ocr.corrections",
            source_dataset_version="1.0", source_dataset_digest="a" * 64,
            trainer_adapter_id="ocr.trainer", trainer_adapter_version="2.1",
        )
        self.assertEqual(artifact, LearningArtifactProvenance.from_dict(artifact.to_dict()))
        artifact.validate_for(trainable_manifest())
        with self.assertRaisesRegex(ValueError, "base model identity/version"):
            LearningArtifactProvenance(
                artifact_id="artifact.delta.02", model_id="other.model", base_model_version="1.2.0",
                artifact_version="1.2.1", artifact_type="ocr.model_delta", source_dataset_id="dataset.run.01",
                source_dataset_schema_id="ocr.corrections",
                source_dataset_version="1.0", source_dataset_digest="b" * 64,
                trainer_adapter_id="ocr.trainer", trainer_adapter_version="2.1",
            ).validate_for(trainable_manifest())

    def test_registry_rejects_duplicate_model_identity(self):
        registry = LearningCapabilityRegistry()
        registry.register(trainable_manifest())
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(trainable_manifest())


if __name__ == "__main__":
    unittest.main()
