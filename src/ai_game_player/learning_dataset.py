from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ai_game_player.experience import (
    ArtifactReference,
    ExperienceEpisode,
    ExperienceEvent,
    ExperienceProvenance,
    ExperienceReader,
)
from ai_game_player.learning import LearningCapabilityManifest, _ID, _VERSION


DATASET_CONTRACT_VERSION = 1
_SAMPLE_TYPES = {
    "decision.sample": "decision.instruction_response",
    "ocr.correction": "ocr.correction",
    "ui.region.label": "ui.region_class",
    "embedding.example": "embedding.example",
    "evaluator.sample": "evaluator.calibration",
}
_LABEL_SOURCES = {"human_correction", "automatic_label", "outcome_evidence", "unattributed"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class DatasetSource:
    episode_id: str
    step_id: str
    event_id: str

    def to_dict(self) -> dict[str, str]:
        return {"episode_id": self.episode_id, "step_id": self.step_id, "event_id": self.event_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DatasetSource:
        _exact_fields(value, {"episode_id", "step_id", "event_id"}, "source")
        return cls(**{name: _string(value[name], name) for name in ("episode_id", "step_id", "event_id")})


@dataclass(frozen=True)
class TrainingSample:
    sample_id: str
    sample_type: str
    model_id: str | None
    model_version: str | None
    input: Any
    target: Any
    relation: str | None
    label_source: str
    label_confidence: float | None
    verified: bool
    provenance: dict[str, Any] | None
    sources: tuple[DatasetSource, ...]
    artifacts: tuple[ArtifactReference, ...]
    sensitive_artifacts_excluded: bool
    split: str

    def __post_init__(self) -> None:
        if not self.sample_id.startswith("sample.") or _SHA256.fullmatch(self.sample_id[7:]) is None:
            raise ValueError("sample_id must contain a SHA-256 fingerprint")
        if self.sample_type not in set(_SAMPLE_TYPES.values()):
            raise ValueError("unsupported sample_type")
        if self.sample_type == "embedding.example":
            if self.relation not in {"positive", "negative", "prototype"}:
                raise ValueError("embedding sample requires a supported relation")
        elif self.relation is not None:
            raise ValueError("relation is only valid for embedding samples")
        if self.model_id is not None and _ID.fullmatch(self.model_id) is None:
            raise ValueError("model_id must be a lowercase identifier")
        if (self.model_id is None) != (self.model_version is None):
            raise ValueError("model_id and model_version must be declared together")
        if self.model_version is not None and _VERSION.fullmatch(self.model_version) is None:
            raise ValueError("model_version must be a version string")
        if self.label_source not in _LABEL_SOURCES:
            raise ValueError("invalid label_source")
        if self.label_confidence is not None and (
            isinstance(self.label_confidence, bool)
            or not isinstance(self.label_confidence, (float, int))
            or not 0 <= self.label_confidence <= 1
        ):
            raise ValueError("label_confidence must be between 0 and 1")
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be a boolean")
        if self.verified and self.label_source != "human_correction":
            raise ValueError("only a human correction can be marked verified")
        if not isinstance(self.sensitive_artifacts_excluded, bool):
            raise ValueError("sensitive_artifacts_excluded must be a boolean")
        if self.split not in {"train", "validation"}:
            raise ValueError("split must be train or validation")
        if not self.sources:
            raise ValueError("sample must retain at least one source reference")
        if any(not isinstance(source, DatasetSource) for source in self.sources):
            raise ValueError("sources must contain DatasetSource values")
        if any(not isinstance(artifact, ArtifactReference) for artifact in self.artifacts):
            raise ValueError("artifacts must contain ArtifactReference values")
        if self.provenance is not None:
            ExperienceProvenance.from_dict(self.provenance)
        _json_value(self.input, "input")
        _json_value(self.target, "target")
        expected_fingerprint = _digest(_sample_content_dict(self))
        if self.sample_id != f"sample.{expected_fingerprint}":
            raise ValueError("sample_id does not match sample content")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "sample_type": self.sample_type,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "input": self.input,
            "target": self.target,
            "relation": self.relation,
            "label_source": self.label_source,
            "label_confidence": self.label_confidence,
            "verified": self.verified,
            "provenance": self.provenance,
            "sources": [item.to_dict() for item in self.sources],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "sensitive_artifacts_excluded": self.sensitive_artifacts_excluded,
            "split": self.split,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TrainingSample:
        fields = {
            "sample_id", "sample_type", "model_id", "model_version", "input", "target", "relation",
            "label_source", "label_confidence", "verified", "provenance", "sources", "artifacts", "split",
            "sensitive_artifacts_excluded",
        }
        _exact_fields(value, fields, "sample")
        sources = value["sources"]
        artifacts = value["artifacts"]
        if not isinstance(sources, list) or not isinstance(artifacts, list):
            raise ValueError("sources and artifacts must be arrays")
        provenance = value["provenance"]
        if provenance is not None and not isinstance(provenance, Mapping):
            raise ValueError("provenance must be an object or null")
        confidence = value["label_confidence"]
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))):
            raise ValueError("label_confidence must be a number or null")
        if any(not isinstance(item, Mapping) for item in artifacts):
            raise ValueError("artifacts must contain objects")
        return cls(
            sample_id=_string(value["sample_id"], "sample_id"),
            sample_type=_string(value["sample_type"], "sample_type"),
            model_id=_optional_string(value["model_id"], "model_id"),
            model_version=_optional_string(value["model_version"], "model_version"),
            input=value["input"], target=value["target"], relation=_optional_string(value["relation"], "relation"),
            label_source=_string(value["label_source"], "label_source"), label_confidence=confidence,
            verified=_boolean(value["verified"], "verified"),
            provenance=None if provenance is None else dict(provenance),
            sources=tuple(DatasetSource.from_dict(item) for item in sources),
            artifacts=tuple(ArtifactReference.from_dict(item) for item in artifacts),
            sensitive_artifacts_excluded=_boolean(value["sensitive_artifacts_excluded"], "sensitive_artifacts_excluded"),
            split=_string(value["split"], "split"),
        )


@dataclass(frozen=True)
class ExperienceTrainingDataset:
    dataset_id: str
    model_id: str
    model_version: str
    schema_id: str
    schema_version: str
    split_seed: int
    validation_fraction: float
    include_sensitive_artifacts: bool
    source_episode_ids: tuple[str, ...]
    samples: tuple[TrainingSample, ...]
    digest: str
    contract_version: int = DATASET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _identifier(self.dataset_id, "dataset_id")
        _identifier(self.model_id, "model_id")
        if _VERSION.fullmatch(self.model_version) is None:
            raise ValueError("model_version must be a version string")
        _identifier(self.schema_id, "schema_id")
        if not isinstance(self.schema_version, str) or _VERSION.fullmatch(self.schema_version) is None:
            raise ValueError("schema_version must be a version string")
        if isinstance(self.split_seed, bool) or not isinstance(self.split_seed, int) or self.split_seed < 0:
            raise ValueError("split_seed must be a non-negative integer")
        if (
            isinstance(self.validation_fraction, bool)
            or not isinstance(self.validation_fraction, (int, float))
            or not 0 < self.validation_fraction < 1
        ):
            raise ValueError("validation_fraction must be between 0 and 1")
        if not isinstance(self.include_sensitive_artifacts, bool):
            raise ValueError("include_sensitive_artifacts must be a boolean")
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("sample IDs must be unique")
        if len(set(self.source_episode_ids)) != len(self.source_episode_ids):
            raise ValueError("source episode IDs must be unique")
        source_episode_set = set(self.source_episode_ids)
        if any(source.episode_id not in source_episode_set for sample in self.samples for source in sample.sources):
            raise ValueError("sample source is missing from source_episode_ids")
        for sample in self.samples:
            if _split_for(sample.sample_id[7:], self.split_seed, self.validation_fraction) != sample.split:
                raise ValueError("sample split does not match the declared seed and fraction")
        if _SHA256.fullmatch(self.digest) is None:
            raise ValueError("digest must be a lowercase SHA-256 hex digest")
        if isinstance(self.contract_version, bool) or not isinstance(self.contract_version, int) or self.contract_version != DATASET_CONTRACT_VERSION:
            raise ValueError(f"unsupported dataset contract version: {self.contract_version}")
        if self.digest != _digest(self._content_dict()):
            raise ValueError("dataset digest does not match content")

    def _content_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "dataset_id": self.dataset_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "split_seed": self.split_seed,
            "validation_fraction": self.validation_fraction,
            "include_sensitive_artifacts": self.include_sensitive_artifacts,
            "source_episode_ids": list(self.source_episode_ids),
            "samples": [sample.to_dict() for sample in self.samples],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_dict(), "digest": self.digest}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExperienceTrainingDataset:
        fields = {
            "contract_version", "dataset_id", "model_id", "model_version", "schema_id", "schema_version", "split_seed",
            "validation_fraction", "include_sensitive_artifacts", "source_episode_ids", "samples", "digest",
        }
        _exact_fields(value, fields, "dataset")
        source_ids = value["source_episode_ids"]
        samples = value["samples"]
        if not isinstance(source_ids, list) or not isinstance(samples, list):
            raise ValueError("source_episode_ids and samples must be arrays")
        fraction = value["validation_fraction"]
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise ValueError("validation_fraction must be a number")
        return cls(
            dataset_id=_string(value["dataset_id"], "dataset_id"),
            model_id=_string(value["model_id"], "model_id"), model_version=_string(value["model_version"], "model_version"),
            schema_id=_string(value["schema_id"], "schema_id"),
            schema_version=_string(value["schema_version"], "schema_version"),
            split_seed=_integer(value["split_seed"], "split_seed"), validation_fraction=fraction,
            include_sensitive_artifacts=_boolean(value["include_sensitive_artifacts"], "include_sensitive_artifacts"),
            source_episode_ids=tuple(_string(item, "source_episode_id") for item in source_ids),
            samples=tuple(TrainingSample.from_dict(item) for item in samples),
            digest=_string(value["digest"], "digest"),
            contract_version=_integer(value["contract_version"], "contract_version"),
        )

    @classmethod
    def from_json(cls, content: str) -> ExperienceTrainingDataset:
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid dataset JSON: {exc}") from exc
        if not isinstance(value, Mapping):
            raise ValueError("dataset must be a JSON object")
        return cls.from_dict(value)


class ExperienceDatasetBuilder:
    """Builds deterministic, deduplicated training samples from Experience events."""

    def __init__(self, manifest: LearningCapabilityManifest) -> None:
        if not isinstance(manifest, LearningCapabilityManifest):
            raise TypeError("manifest must be a LearningCapabilityManifest")
        if not manifest.trainable:
            raise ValueError(f"model {manifest.model_id} is not trainable: {manifest.untrainable_reason}")
        assert manifest.dataset_schema_id is not None and manifest.dataset_schema_version is not None
        self.manifest = manifest
        self.schema_id = manifest.dataset_schema_id
        self.schema_version = manifest.dataset_schema_version

    def build(
        self,
        episodes: Iterable[ExperienceEpisode] | ExperienceReader,
        *,
        split_seed: int = 0,
        validation_fraction: float = 0.2,
        include_sensitive_artifacts: bool = False,
    ) -> ExperienceTrainingDataset:
        episode_source = episodes.iter_episodes() if isinstance(episodes, ExperienceReader) else episodes
        episode_list = tuple(sorted(episode_source, key=lambda item: item.episode_id))
        if len({item.episode_id for item in episode_list}) != len(episode_list):
            raise ValueError("duplicate source episode ID")
        collected: dict[str, dict[str, Any]] = {}
        for episode in episode_list:
            for step in sorted(episode.steps, key=lambda item: item.sequence):
                for event in step.events:
                    sample_type = _SAMPLE_TYPES.get(event.event_type)
                    if sample_type is None:
                        continue
                    record = self._sample_record(
                        episode, step.step_id, event, sample_type, split_seed,
                        validation_fraction, include_sensitive_artifacts,
                    )
                    fingerprint = _digest({key: value for key, value in record.items() if key != "split"})
                    existing = collected.get(fingerprint)
                    source = DatasetSource(episode.episode_id, step.step_id, event.event_id)
                    if existing is None:
                        collected[fingerprint] = {**record, "fingerprint": fingerprint, "sources": {source}}
                    else:
                        existing["sources"].add(source)

        samples = tuple(
            TrainingSample(
                sample_id=f"sample.{fingerprint}", sample_type=record["sample_type"],
                model_id=record["model_id"], model_version=record["model_version"],
                input=record["input"], target=record["target"], relation=record["relation"],
                label_source=record["label_source"], label_confidence=record["label_confidence"],
                verified=record["verified"], provenance=record["provenance"],
                sources=tuple(sorted(record["sources"])),
                artifacts=tuple(ArtifactReference.from_dict(item) for item in record["artifacts"]),
                sensitive_artifacts_excluded=record["sensitive_artifacts_excluded"],
                split=record["split"],
            )
            for fingerprint, record in sorted(collected.items())
        )
        source_episode_ids = tuple(item.episode_id for item in episode_list)
        identity = _digest({
            "model_id": self.manifest.model_id, "model_version": self.manifest.model_version,
            "schema_id": self.schema_id, "schema_version": self.schema_version,
            "split_seed": split_seed, "validation_fraction": validation_fraction,
            "include_sensitive_artifacts": include_sensitive_artifacts,
            "source_episode_ids": list(source_episode_ids),
            "samples": [sample.to_dict() for sample in samples],
        })
        dataset_id = f"dataset.{identity[:24]}"
        content = {
            "contract_version": DATASET_CONTRACT_VERSION, "dataset_id": dataset_id,
            "model_id": self.manifest.model_id, "model_version": self.manifest.model_version,
            "schema_id": self.schema_id, "schema_version": self.schema_version,
            "split_seed": split_seed, "validation_fraction": validation_fraction,
            "include_sensitive_artifacts": include_sensitive_artifacts,
            "source_episode_ids": list(source_episode_ids), "samples": [sample.to_dict() for sample in samples],
        }
        return ExperienceTrainingDataset(
            dataset_id=dataset_id, model_id=self.manifest.model_id, model_version=self.manifest.model_version,
            schema_id=self.schema_id, schema_version=self.schema_version,
            split_seed=split_seed, validation_fraction=validation_fraction,
            include_sensitive_artifacts=include_sensitive_artifacts, source_episode_ids=source_episode_ids,
            samples=samples, digest=_digest(content),
        )

    def _sample_record(
        self,
        episode: ExperienceEpisode,
        step_id: str,
        event: ExperienceEvent,
        sample_type: str,
        split_seed: int,
        validation_fraction: float,
        include_sensitive_artifacts: bool,
    ) -> dict[str, Any]:
        payload = event.payload
        allowed = {"input", "target", "relation", "label_source", "label_confidence", "verified"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unsupported fields in {event.event_type} sample: {', '.join(sorted(unknown))}")
        if "input" not in payload or "target" not in payload:
            raise ValueError(f"{event.event_type} event must contain input and target")
        relation = None
        if sample_type == "embedding.example":
            relation = _string(payload.get("relation"), "relation")
            if relation not in {"positive", "negative", "prototype"}:
                raise ValueError("embedding relation must be positive, negative, or prototype")
        elif payload.get("relation") is not None:
            raise ValueError("relation is only valid for embedding samples")
        model_id = event.model_id or (event.provenance.model_id if event.provenance is not None else None)
        model_version = event.model_version or (event.provenance.model_version if event.provenance is not None else None)
        label_source = _label_source(payload.get("label_source"), event.provenance)
        confidence = payload.get("label_confidence")
        if confidence is None and event.provenance is not None:
            confidence = event.provenance.confidence
        if confidence is not None and (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1
        ):
            raise ValueError("label_confidence must be between 0 and 1")
        verified = payload.get("verified", False)
        if not isinstance(verified, bool):
            raise ValueError("verified must be a boolean")
        if verified and label_source != "human_correction":
            raise ValueError("only a human correction can be marked verified")
        sensitive_artifacts_excluded = not include_sensitive_artifacts and any(item.sensitive for item in event.artifacts)
        artifacts = [item.to_dict() for item in event.artifacts if include_sensitive_artifacts or not item.sensitive]
        unsigned = {
            "sample_type": sample_type, "model_id": model_id, "model_version": model_version,
            "input": payload["input"], "target": payload["target"], "relation": relation,
            "label_source": label_source, "label_confidence": confidence, "verified": verified,
            "provenance": None if event.provenance is None else event.provenance.to_dict(),
            "artifacts": artifacts, "sensitive_artifacts_excluded": sensitive_artifacts_excluded,
        }
        fingerprint = _digest(unsigned)
        unsigned["split"] = _split_for(fingerprint, split_seed, validation_fraction)
        return unsigned


def _label_source(value: Any, provenance: ExperienceProvenance | None) -> str:
    candidate = value
    if candidate is None and provenance is not None:
        candidate = provenance.source
    return candidate if isinstance(candidate, str) and candidate in _LABEL_SOURCES else "unattributed"


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sample_content_dict(sample: TrainingSample) -> dict[str, Any]:
    return {
        "sample_type": sample.sample_type,
        "model_id": sample.model_id,
        "model_version": sample.model_version,
        "input": sample.input,
        "target": sample.target,
        "relation": sample.relation,
        "label_source": sample.label_source,
        "label_confidence": sample.label_confidence,
        "verified": sample.verified,
        "provenance": sample.provenance,
        "artifacts": [item.to_dict() for item in sample.artifacts],
        "sensitive_artifacts_excluded": sample.sensitive_artifacts_excluded,
    }


def _split_for(fingerprint: str, seed: int, validation_fraction: float) -> str:
    split_value = int(hashlib.sha256(f"{seed}:{fingerprint}".encode("ascii")).hexdigest()[:16], 16) / 0xFFFFFFFFFFFFFFFF
    return "validation" if split_value < validation_fraction else "train"


def _json_value(value: Any, name: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON-compatible values") from exc


def _identifier(value: Any, name: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase identifier")


def _exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing: {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown: {', '.join(sorted(unknown))}")
        raise ValueError(f"invalid {name} fields (" + "; ".join(details) + ")")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value
