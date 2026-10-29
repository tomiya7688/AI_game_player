# Learning Capability Contract

`LearningCapabilityManifest` in `src/ai_game_player/learning.py` is the versioned declaration for a model profile's learning support. Its machine-readable shape is `config/learning_capability.schema.json`; runtime parsing and compatibility checks do not add a JSON Schema dependency. Serialized `LearningArtifactProvenance` follows `config/learning_artifact_provenance.schema.json`.

A trainable profile declares at least one update strategy, the exact Dataset schema/version, trainer adapter/version, runtime requirements, output artifact type, evaluation suite, promotion policy, and rollback source. `LearningCapabilityRegistry.resolve(model_id)` returns this metadata without starting training. A profile that cannot be trained remains resolvable with `trainable: false` and a user-facing `untrainable_reason`; it cannot declare a partial trainer contract.

Before dispatch, `validate_compatibility` rejects mismatched dataset/trainer/artifact versions and missing runtime requirements. `LearningArtifactProvenance` records artifact identity/version, base model/version, source Dataset identity/schema/version and SHA-256 digest, and trainer identity/version, then validates the artifact against its manifest. This Issue defines metadata and compatibility only: Dataset creation, trainer execution, evaluation, promotion, and rollback remain separate child Issues under #180.

Example trainable manifest:

```json
{
  "schema_version": 1,
  "model_id": "ocr.sample",
  "model_version": "1.2.0",
  "trainable": true,
  "update_strategies": ["corrected_samples"],
  "dataset_schema_id": "ocr.corrections",
  "dataset_schema_version": "1.0",
  "trainer_adapter_id": "ocr.trainer",
  "trainer_adapter_version": "2.1",
  "required_runtime": ["cpu"],
  "output_artifact_type": "ocr.model_delta",
  "evaluation_suite": "ocr.regression",
  "promotion_policy": "no_regression",
  "rollback_source": "champion_pointer",
  "untrainable_reason": null
}
```
