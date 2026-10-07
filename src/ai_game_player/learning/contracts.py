from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


LEARNING_CONTRACT_VERSION = 1
_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][a-zA-Z0-9.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class LearningCapabilityManifest:
    model_id: str
    model_version: str
    trainable: bool
    update_strategies: frozenset[str]
    dataset_schema_id: str | None
    dataset_schema_version: str | None
    trainer_adapter_id: str | None
    trainer_adapter_version: str | None
    required_runtime: frozenset[str]
    output_artifact_type: str | None
    evaluation_suite: str | None
    promotion_policy: str | None
    rollback_source: str | None
    untrainable_reason: str | None = None
    schema_version: int = LEARNING_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.trainable, bool):
            raise ValueError("trainable must be a boolean")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise ValueError("schema_version must be an integer")
        _identifier(self.model_id, "model_id")
        _version(self.model_version, "model_version")
        if self.schema_version != LEARNING_CONTRACT_VERSION:
            raise ValueError(f"unsupported learning contract version: {self.schema_version}")
        if not isinstance(self.update_strategies, (set, frozenset)):
            raise ValueError("update_strategies must be a set of strings")
        if not isinstance(self.required_runtime, (set, frozenset)):
            raise ValueError("required_runtime must be a set of strings")
        for strategy in self.update_strategies:
            _identifier(strategy, "update_strategy")
        for requirement in self.required_runtime:
            _identifier(requirement, "required_runtime")
        if self.trainable:
            if not self.update_strategies:
                raise ValueError("trainable model must declare at least one update strategy")
            required = {
                "dataset_schema_id": self.dataset_schema_id,
                "dataset_schema_version": self.dataset_schema_version,
                "trainer_adapter_id": self.trainer_adapter_id,
                "trainer_adapter_version": self.trainer_adapter_version,
                "output_artifact_type": self.output_artifact_type,
                "evaluation_suite": self.evaluation_suite,
                "promotion_policy": self.promotion_policy,
                "rollback_source": self.rollback_source,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"trainable manifest is missing: {', '.join(missing)}")
            _identifier(self.dataset_schema_id, "dataset_schema_id")
            _version(self.dataset_schema_version, "dataset_schema_version")
            _identifier(self.trainer_adapter_id, "trainer_adapter_id")
            _version(self.trainer_adapter_version, "trainer_adapter_version")
            _identifier(self.output_artifact_type, "output_artifact_type")
            _identifier(self.evaluation_suite, "evaluation_suite")
            _identifier(self.promotion_policy, "promotion_policy")
            _identifier(self.rollback_source, "rollback_source")
            if self.untrainable_reason is not None:
                raise ValueError("trainable manifest cannot declare an untrainable_reason")
        else:
            if not isinstance(self.untrainable_reason, str) or not self.untrainable_reason.strip():
                raise ValueError("untrainable model must declare an untrainable_reason")
            training_fields = {
                "update_strategies": self.update_strategies,
                "dataset_schema_id": self.dataset_schema_id,
                "dataset_schema_version": self.dataset_schema_version,
                "trainer_adapter_id": self.trainer_adapter_id,
                "trainer_adapter_version": self.trainer_adapter_version,
                "output_artifact_type": self.output_artifact_type,
                "evaluation_suite": self.evaluation_suite,
                "promotion_policy": self.promotion_policy,
                "rollback_source": self.rollback_source,
            }
            if any(training_fields.values()):
                raise ValueError("untrainable manifest cannot declare training metadata")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LearningCapabilityManifest:
        allowed = {
            "schema_version", "model_id", "model_version", "trainable", "update_strategies",
            "dataset_schema_id", "dataset_schema_version", "trainer_adapter_id", "trainer_adapter_version",
            "required_runtime", "output_artifact_type", "evaluation_suite", "promotion_policy",
            "rollback_source", "untrainable_reason",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown learning manifest fields: {', '.join(sorted(unknown))}")
        required = allowed
        missing = required - set(value)
        if missing:
            raise ValueError(f"missing learning manifest fields: {', '.join(sorted(missing))}")
        if not isinstance(value["trainable"], bool):
            raise ValueError("trainable must be a boolean")
        return cls(
            schema_version=_integer(value["schema_version"], "schema_version"),
            model_id=_string(value["model_id"], "model_id"),
            model_version=_string(value["model_version"], "model_version"),
            trainable=value["trainable"],
            update_strategies=_string_set(value["update_strategies"], "update_strategies"),
            dataset_schema_id=_optional_string(value.get("dataset_schema_id"), "dataset_schema_id"),
            dataset_schema_version=_optional_string(value.get("dataset_schema_version"), "dataset_schema_version"),
            trainer_adapter_id=_optional_string(value.get("trainer_adapter_id"), "trainer_adapter_id"),
            trainer_adapter_version=_optional_string(value.get("trainer_adapter_version"), "trainer_adapter_version"),
            required_runtime=_string_set(value["required_runtime"], "required_runtime"),
            output_artifact_type=_optional_string(value.get("output_artifact_type"), "output_artifact_type"),
            evaluation_suite=_optional_string(value.get("evaluation_suite"), "evaluation_suite"),
            promotion_policy=_optional_string(value.get("promotion_policy"), "promotion_policy"),
            rollback_source=_optional_string(value.get("rollback_source"), "rollback_source"),
            untrainable_reason=_optional_string(value.get("untrainable_reason"), "untrainable_reason"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "trainable": self.trainable,
            "update_strategies": sorted(self.update_strategies),
            "dataset_schema_id": self.dataset_schema_id,
            "dataset_schema_version": self.dataset_schema_version,
            "trainer_adapter_id": self.trainer_adapter_id,
            "trainer_adapter_version": self.trainer_adapter_version,
            "required_runtime": sorted(self.required_runtime),
            "output_artifact_type": self.output_artifact_type,
            "evaluation_suite": self.evaluation_suite,
            "promotion_policy": self.promotion_policy,
            "rollback_source": self.rollback_source,
            "untrainable_reason": self.untrainable_reason,
        }

    def validate_compatibility(
        self,
        *,
        dataset_schema_id: str,
        dataset_schema_version: str,
        trainer_adapter_id: str,
        trainer_adapter_version: str,
        output_artifact_type: str,
        available_runtime: frozenset[str] | set[str] | None = None,
    ) -> None:
        if not self.trainable:
            raise ValueError(f"model {self.model_id} is not trainable: {self.untrainable_reason}")
        expected = {
            "dataset schema id": (self.dataset_schema_id, dataset_schema_id),
            "dataset schema version": (self.dataset_schema_version, dataset_schema_version),
            "trainer adapter id": (self.trainer_adapter_id, trainer_adapter_id),
            "trainer adapter version": (self.trainer_adapter_version, trainer_adapter_version),
            "output artifact type": (self.output_artifact_type, output_artifact_type),
        }
        mismatches = [name for name, (declared, actual) in expected.items() if declared != actual]
        if mismatches:
            raise ValueError(f"incompatible learning contract: {', '.join(mismatches)}")
        if available_runtime is not None:
            missing = self.required_runtime - frozenset(available_runtime)
            if missing:
                raise ValueError(f"missing runtime requirements: {', '.join(sorted(missing))}")


@dataclass(frozen=True)
class LearningArtifactProvenance:
    artifact_id: str
    model_id: str
    base_model_version: str
    artifact_version: str
    artifact_type: str
    source_dataset_id: str
    source_dataset_schema_id: str
    source_dataset_version: str
    source_dataset_digest: str
    trainer_adapter_id: str
    trainer_adapter_version: str
    schema_version: int = LEARNING_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise ValueError("schema_version must be an integer")
        if self.schema_version != LEARNING_CONTRACT_VERSION:
            raise ValueError(f"unsupported artifact provenance schema version: {self.schema_version}")
        for name in (
            "artifact_id", "model_id", "artifact_type", "source_dataset_id", "source_dataset_schema_id",
            "trainer_adapter_id",
        ):
            _identifier(getattr(self, name), name)
        for name in ("base_model_version", "artifact_version", "source_dataset_version", "trainer_adapter_version"):
            _version(getattr(self, name), name)
        if _SHA256.fullmatch(self.source_dataset_digest) is None:
            raise ValueError("source_dataset_digest must be a lowercase SHA-256 hex digest")

    def validate_for(self, manifest: LearningCapabilityManifest) -> None:
        manifest.validate_compatibility(
            dataset_schema_id=self.source_dataset_schema_id,
            dataset_schema_version=self.source_dataset_version,
            trainer_adapter_id=self.trainer_adapter_id,
            trainer_adapter_version=self.trainer_adapter_version,
            output_artifact_type=self.artifact_type,
        )
        if self.model_id != manifest.model_id or self.base_model_version != manifest.model_version:
            raise ValueError("artifact provenance does not match base model identity/version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "model_id": self.model_id,
            "base_model_version": self.base_model_version,
            "artifact_version": self.artifact_version,
            "artifact_type": self.artifact_type,
            "source_dataset_id": self.source_dataset_id,
            "source_dataset_schema_id": self.source_dataset_schema_id,
            "source_dataset_version": self.source_dataset_version,
            "source_dataset_digest": self.source_dataset_digest,
            "trainer_adapter_id": self.trainer_adapter_id,
            "trainer_adapter_version": self.trainer_adapter_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LearningArtifactProvenance:
        fields = {
            "schema_version",
            "artifact_id", "model_id", "base_model_version", "artifact_version", "artifact_type",
            "source_dataset_id", "source_dataset_schema_id", "source_dataset_version",
            "source_dataset_digest", "trainer_adapter_id", "trainer_adapter_version",
        }
        unknown = set(value) - fields
        missing = fields - set(value)
        if unknown:
            raise ValueError(f"unknown artifact provenance fields: {', '.join(sorted(unknown))}")
        if missing:
            raise ValueError(f"missing artifact provenance fields: {', '.join(sorted(missing))}")
        parsed: dict[str, Any] = {name: _string(value[name], name) for name in fields - {"schema_version"}}
        parsed["schema_version"] = _integer(value["schema_version"], "schema_version")
        return cls(**parsed)


class LearningCapabilityRegistry:
    def __init__(self) -> None:
        self._manifests: dict[str, LearningCapabilityManifest] = {}

    def register(self, manifest: LearningCapabilityManifest) -> None:
        if manifest.model_id in self._manifests:
            raise ValueError(f"learning capability already registered: {manifest.model_id}")
        self._manifests[manifest.model_id] = manifest

    def resolve(self, model_id: str) -> LearningCapabilityManifest:
        try:
            return self._manifests[model_id]
        except KeyError as exc:
            raise KeyError(f"learning capability not found: {model_id}") from exc

    def manifests(self) -> tuple[LearningCapabilityManifest, ...]:
        return tuple(self._manifests.values())


def _identifier(value: str | None, field_name: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase identifier")


def _version(value: str | None, field_name: str) -> None:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a version string")


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    return None if value is None else _string(value, field_name)


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _string_set(value: Any, field_name: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must not contain duplicates")
    return frozenset(value)
