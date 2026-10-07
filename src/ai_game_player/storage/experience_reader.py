from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_game_player.storage.artifact_store import ArtifactStore
from ai_game_player.storage.experience import (
    EXPERIENCE_SCHEMA_VERSION,
    ArtifactReference,
    ExperienceEpisode,
    ExperienceStep,
)


@dataclass(frozen=True)
class RecoveryNotice:
    path: Path
    message: str


class ExperienceRecoveryError(ValueError):
    """An Experience file cannot be recovered without discarding valid records."""


class JsonlExperienceReader:
    """Indexed JSONL reader for canonical episodes, with valid-prefix recovery.

    Each non-empty line is one complete episode. A malformed final line is
    treated as a crash-truncated tail; malformed interior lines fail closed.
    An optional ``<file>.checkpoint.json`` records the byte digest and line
    count up to a checkpoint and is validated before being trusted.
    """

    def __init__(self, path: Path, *, artifact_store: ArtifactStore | None = None) -> None:
        self.path = Path(path)
        self.artifact_store = artifact_store
        self.recovery_notices: list[RecoveryNotice] = []
        self.checkpoint_valid: bool | None = None
        self._episodes: dict[str, ExperienceEpisode] = {}
        self._load()

    def get_episode(self, episode_id: str) -> ExperienceEpisode | None:
        return self._episodes.get(episode_id)

    def iter_episodes(self) -> Iterator[ExperienceEpisode]:
        return iter(self._episodes.values())

    def get_step(self, episode_id: str, step_id: str) -> ExperienceStep | None:
        episode = self.get_episode(episode_id)
        if episode is None:
            return None
        return next((step for step in episode.steps if step.step_id == step_id), None)

    def find(
        self,
        *,
        source: str | None = None,
        model_id: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> tuple[ExperienceEpisode, ...]:
        after = _parse_timestamp(created_after) if created_after is not None else None
        before = _parse_timestamp(created_before) if created_before is not None else None
        return tuple(
            episode for episode in self._episodes.values()
            if (source is None or episode.source == source)
            and (model_id is None or episode.model_id == model_id)
            and (after is None or _parse_timestamp(episode.created_at) >= after)
            and (before is None or _parse_timestamp(episode.created_at) <= before)
        )

    def reconstruct_state(self, episode_id: str, step_id: str) -> dict[str, Any] | None:
        """Rebuild state through the requested step from snapshot/delta events.

        Snapshot event payload: ``{"state": {...}}``. Delta payload:
        ``{"set": {...}, "remove": ["key", ...]}``. Unknown or malformed
        state events raise ValueError rather than returning a partial state.
        """
        episode = self.get_episode(episode_id)
        if episode is None:
            return None
        ordered = sorted(episode.steps, key=lambda item: item.sequence)
        target_index = next((i for i, step in enumerate(ordered) if step.step_id == step_id), None)
        if target_index is None:
            return None

        state: dict[str, Any] | None = None
        for step in ordered[: target_index + 1]:
            for event in step.events:
                if event.event_type == "state.snapshot":
                    payload = event.payload
                    if set(payload) != {"state"} or not isinstance(payload["state"], Mapping):
                        raise ValueError(f"invalid state snapshot in event {event.event_id}")
                    state = dict(payload["state"])
                elif event.event_type == "state.delta":
                    if state is None:
                        raise ValueError(f"state delta precedes a snapshot in event {event.event_id}")
                    payload = event.payload
                    if set(payload) != {"set", "remove"} or not isinstance(payload["set"], Mapping):
                        raise ValueError(f"invalid state delta in event {event.event_id}")
                    remove = payload["remove"]
                    if not isinstance(remove, list) or any(not isinstance(key, str) for key in remove):
                        raise ValueError(f"invalid state delta removals in event {event.event_id}")
                    for key in remove:
                        state.pop(key, None)
                    state.update(payload["set"])
        return None if state is None else dict(state)

    def _load(self) -> None:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        raw = self.path.read_bytes()
        self._validate_checkpoint(raw)
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                if index == len(lines) - 1:
                    self.recovery_notices.append(RecoveryNotice(self.path, f"ignored corrupt trailing record: {exc}"))
                    break
                raise ExperienceRecoveryError(f"invalid interior record at line {index + 1}: {exc}") from exc
            try:
                if not isinstance(value, Mapping):
                    raise ValueError("episode record must be an object")
                episode = ExperienceEpisode.from_dict(migrate_experience_dict(value))
            except (TypeError, ValueError) as exc:
                raise ExperienceRecoveryError(f"invalid episode at line {index + 1}: {exc}") from exc
            if episode.episode_id in self._episodes:
                raise ExperienceRecoveryError(f"duplicate episode ID: {episode.episode_id}")
            self._validate_artifacts(episode)
            self._episodes[episode.episode_id] = episode

    def _validate_checkpoint(self, raw: bytes) -> None:
        checkpoint_path = Path(str(self.path) + ".checkpoint.json")
        if not checkpoint_path.exists():
            self.checkpoint_valid = None
            return
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(checkpoint, Mapping) or set(checkpoint) != {"line_count", "sha256"}:
                raise ValueError("checkpoint fields are invalid")
            count = checkpoint["line_count"]
            digest = checkpoint["sha256"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("checkpoint line_count is invalid")
            lines = raw.splitlines(keepends=True)
            prefix = b"".join(lines[:count])
            self.checkpoint_valid = len(lines) >= count and hashlib.sha256(prefix).hexdigest() == digest
            if not self.checkpoint_valid:
                self.recovery_notices.append(RecoveryNotice(checkpoint_path, "checkpoint did not match the data; rebuilt by scanning records"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            self.checkpoint_valid = False
            self.recovery_notices.append(RecoveryNotice(checkpoint_path, f"invalid checkpoint ignored: {exc}"))

    def _validate_artifacts(self, episode: ExperienceEpisode) -> None:
        if self.artifact_store is None:
            return
        for reference in iter_artifact_references(episode):
            self.artifact_store.verify(reference)


def migrate_experience_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate the pre-canonical (version 0) JSON shape to schema version 1.

    Version 0 records omitted schema_version and the newer optional provenance,
    artifact, and model fields. Unknown fields are preserved for the strict v1
    parser to reject, so migration cannot silently lose data.
    """
    migrated = dict(value)
    version = migrated.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("schema_version must be an integer")
    if version == EXPERIENCE_SCHEMA_VERSION:
        return migrated
    if version != 0:
        raise ValueError(f"unsupported experience schema version: {version}")
    migrated["schema_version"] = EXPERIENCE_SCHEMA_VERSION
    migrated.setdefault("model_id", None)
    migrated.setdefault("model_version", None)
    migrated.setdefault("artifacts", [])
    migrated.setdefault("provenance", None)
    steps = migrated.get("steps", [])
    if not isinstance(steps, list):
        return migrated
    migrated["steps"] = [_migrate_step(step) if isinstance(step, Mapping) else step for step in steps]
    return migrated


def _migrate_step(value: Mapping[str, Any]) -> dict[str, Any]:
    step = dict(value)
    step.setdefault("model_id", None)
    step.setdefault("model_version", None)
    step.setdefault("artifacts", [])
    step.setdefault("provenance", None)
    events = step.get("events", [])
    if isinstance(events, list):
        step["events"] = [_migrate_event(event) if isinstance(event, Mapping) else event for event in events]
    return step


def _migrate_event(value: Mapping[str, Any]) -> dict[str, Any]:
    event = dict(value)
    event.setdefault("payload", {})
    event.setdefault("model_id", None)
    event.setdefault("model_version", None)
    event.setdefault("artifacts", [])
    event.setdefault("provenance", None)
    return event


def iter_artifact_references(episode: ExperienceEpisode) -> Iterable[ArtifactReference]:
    yield from episode.artifacts
    for step in episode.steps:
        yield from step.artifacts
        for event in step.events:
            yield from event.artifacts


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("query timestamps must be timezone-qualified ISO-8601 values") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("query timestamps must include a timezone")
    return parsed
