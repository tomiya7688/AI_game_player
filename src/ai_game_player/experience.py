from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


EXPERIENCE_SCHEMA_VERSION = 1
_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    sha256: str
    media_type: str
    sensitive: bool

    def __post_init__(self) -> None:
        _identifier(self.artifact_id, "artifact_id")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be a lowercase SHA-256 hex digest")
        if (
            not isinstance(self.media_type, str)
            or self.media_type.count("/") != 1
            or any(character.isspace() for character in self.media_type)
            or any(not part for part in self.media_type.split("/"))
        ):
            raise ValueError("media_type must be a MIME type")
        if not isinstance(self.sensitive, bool):
            raise ValueError("sensitive must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact_id, "sha256": self.sha256, "media_type": self.media_type, "sensitive": self.sensitive}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactReference:
        _fields(value, {"artifact_id", "sha256", "media_type", "sensitive"}, "artifact reference")
        return cls(
            artifact_id=_string(value["artifact_id"], "artifact_id"),
            sha256=_string(value["sha256"], "sha256"),
            media_type=_string(value["media_type"], "media_type"),
            sensitive=_boolean(value["sensitive"], "sensitive"),
        )


@dataclass(frozen=True)
class ExperienceProvenance:
    source: str
    actor_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    confidence: float | None = None
    evidence_event_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.source, "provenance.source")
        for name in ("actor_id", "model_id", "evidence_event_id"):
            value = getattr(self, name)
            if value is not None:
                _identifier(value, f"provenance.{name}")
        if (self.model_id is None) != (self.model_version is None):
            raise ValueError("provenance model_id and model_version must be declared together")
        if self.model_version is not None:
            _version(self.model_version, "provenance.model_version")
        if self.confidence is not None and (
            isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float))
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("provenance.confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "actor_id": self.actor_id, "model_id": self.model_id,
            "model_version": self.model_version, "confidence": self.confidence,
            "evidence_event_id": self.evidence_event_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExperienceProvenance:
        _fields(value, {"source", "actor_id", "model_id", "model_version", "confidence", "evidence_event_id"}, "provenance")
        confidence = value["confidence"]
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (float, int))):
            raise ValueError("provenance.confidence must be a number or null")
        return cls(
            source=_string(value["source"], "source"),
            actor_id=_optional_string(value["actor_id"], "actor_id"),
            model_id=_optional_string(value["model_id"], "model_id"),
            model_version=_optional_string(value["model_version"], "model_version"),
            confidence=confidence,
            evidence_event_id=_optional_string(value["evidence_event_id"], "evidence_event_id"),
        )


@dataclass(frozen=True)
class ExperienceEvent:
    event_id: str
    event_type: str
    timestamp: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    model_id: str | None = None
    model_version: str | None = None
    artifacts: tuple[ArtifactReference, ...] = ()
    provenance: ExperienceProvenance | None = None

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        _identifier(self.event_type, "event_type")
        _identifier(self.source, "source")
        _timestamp(self.timestamp, "timestamp")
        _model_version_pair(self.model_id, self.model_version)
        _json_object(self.payload, "payload")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "event_type": self.event_type, "timestamp": self.timestamp,
            "source": self.source, "payload": dict(self.payload), "model_id": self.model_id,
            "model_version": self.model_version, "artifacts": [item.to_dict() for item in self.artifacts],
            "provenance": None if self.provenance is None else self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExperienceEvent:
        _fields(value, {"event_id", "event_type", "timestamp", "source", "payload", "model_id", "model_version", "artifacts", "provenance"}, "event")
        return cls(
            event_id=_string(value["event_id"], "event_id"), event_type=_string(value["event_type"], "event_type"),
            timestamp=_string(value["timestamp"], "timestamp"), source=_string(value["source"], "source"),
            payload=_mapping(value["payload"], "payload"), model_id=_optional_string(value["model_id"], "model_id"),
            model_version=_optional_string(value["model_version"], "model_version"),
            artifacts=_artifact_refs(value["artifacts"]), provenance=_optional_provenance(value["provenance"]),
        )


@dataclass(frozen=True)
class ExperienceStep:
    step_id: str
    sequence: int
    timestamp: str
    source: str
    events: tuple[ExperienceEvent, ...] = ()
    model_id: str | None = None
    model_version: str | None = None
    artifacts: tuple[ArtifactReference, ...] = ()
    provenance: ExperienceProvenance | None = None

    def __post_init__(self) -> None:
        _identifier(self.step_id, "step_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("step sequence must be a non-negative integer")
        _timestamp(self.timestamp, "timestamp")
        _identifier(self.source, "source")
        _model_version_pair(self.model_id, self.model_version)
        event_ids = [event.event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("event IDs must be unique within a step")

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id, "sequence": self.sequence, "timestamp": self.timestamp,
            "source": self.source, "events": [item.to_dict() for item in self.events],
            "model_id": self.model_id, "model_version": self.model_version,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "provenance": None if self.provenance is None else self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExperienceStep:
        _fields(value, {"step_id", "sequence", "timestamp", "source", "events", "model_id", "model_version", "artifacts", "provenance"}, "step")
        return cls(
            step_id=_string(value["step_id"], "step_id"), sequence=_integer(value["sequence"], "sequence"),
            timestamp=_string(value["timestamp"], "timestamp"), source=_string(value["source"], "source"),
            events=_events(value["events"]), model_id=_optional_string(value["model_id"], "model_id"),
            model_version=_optional_string(value["model_version"], "model_version"),
            artifacts=_artifact_refs(value["artifacts"]), provenance=_optional_provenance(value["provenance"]),
        )


@dataclass(frozen=True)
class ExperienceEpisode:
    episode_id: str
    created_at: str
    source: str
    steps: tuple[ExperienceStep, ...] = ()
    model_id: str | None = None
    model_version: str | None = None
    artifacts: tuple[ArtifactReference, ...] = ()
    provenance: ExperienceProvenance | None = None
    schema_version: int = EXPERIENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.episode_id, "episode_id")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != EXPERIENCE_SCHEMA_VERSION
        ):
            raise ValueError(f"unsupported experience schema version: {self.schema_version}")
        _timestamp(self.created_at, "created_at")
        _identifier(self.source, "source")
        _model_version_pair(self.model_id, self.model_version)
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step IDs must be unique within an episode")
        sequences = [step.sequence for step in self.steps]
        if len(set(sequences)) != len(sequences):
            raise ValueError("step sequence numbers must be unique within an episode")
        event_ids = [event.event_id for step in self.steps for event in step.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("event IDs must be unique within an episode")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "episode_id": self.episode_id,
            "created_at": self.created_at, "source": self.source,
            "model_id": self.model_id, "model_version": self.model_version,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "provenance": None if self.provenance is None else self.provenance.to_dict(),
            "steps": [item.to_dict() for item in self.steps],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExperienceEpisode:
        _fields(value, {"schema_version", "episode_id", "created_at", "source", "model_id", "model_version", "artifacts", "provenance", "steps"}, "episode")
        return cls(
            schema_version=_integer(value["schema_version"], "schema_version"),
            episode_id=_string(value["episode_id"], "episode_id"), created_at=_string(value["created_at"], "created_at"),
            source=_string(value["source"], "source"), model_id=_optional_string(value["model_id"], "model_id"),
            model_version=_optional_string(value["model_version"], "model_version"),
            artifacts=_artifact_refs(value["artifacts"]), provenance=_optional_provenance(value["provenance"]),
            steps=_steps(value["steps"]),
        )


@runtime_checkable
class ExperienceReader(Protocol):
    def get_episode(self, episode_id: str) -> ExperienceEpisode | None: ...

    def iter_episodes(self) -> Iterator[ExperienceEpisode]: ...


class InMemoryExperienceReader:
    """Logical reader adapter useful for tests and storage backends."""

    def __init__(self, episodes: Iterable[ExperienceEpisode] = ()) -> None:
        self._episodes: dict[str, ExperienceEpisode] = {}
        for episode in episodes:
            if episode.episode_id in self._episodes:
                raise ValueError(f"duplicate episode ID: {episode.episode_id}")
            self._episodes[episode.episode_id] = episode

    def get_episode(self, episode_id: str) -> ExperienceEpisode | None:
        return self._episodes.get(episode_id)

    def iter_episodes(self) -> Iterator[ExperienceEpisode]:
        return iter(self._episodes.values())


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase identifier")


def _version(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a version string")


def _timestamp(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")


def _json_object(value: Mapping[str, Any], field_name: str) -> None:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be an object with string keys")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-compatible values") from exc


def _fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise ValueError(f"{name} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _model_version_pair(model_id: str | None, model_version: str | None) -> None:
    if (model_id is None) != (model_version is None):
        raise ValueError("model_id and model_version must be declared together")
    if model_id is not None:
        _identifier(model_id, "model_id")
        assert model_version is not None
        _version(model_version, "model_version")


def _artifact_refs(value: Any) -> tuple[ArtifactReference, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("artifacts must be an array")
    return tuple(ArtifactReference.from_dict(item) for item in value)


def _optional_provenance(value: Any) -> ExperienceProvenance | None:
    return None if value is None else ExperienceProvenance.from_dict(_mapping(value, "provenance"))


def _events(value: Any) -> tuple[ExperienceEvent, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("events must be an array")
    return tuple(ExperienceEvent.from_dict(item) for item in value)


def _steps(value: Any) -> tuple[ExperienceStep, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("steps must be an array")
    return tuple(ExperienceStep.from_dict(item) for item in value)
