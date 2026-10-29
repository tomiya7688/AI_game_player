from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ai_game_player.experience import ArtifactReference


ARTIFACT_METADATA_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")


class ArtifactIntegrityError(ValueError):
    """Stored bytes or metadata do not match an artifact reference."""


@dataclass(frozen=True)
class ArtifactMetadata:
    sha256: str
    size_bytes: int
    media_type: str
    artifact_type: str
    sensitive: bool
    created_at: str
    schema_version: int = ARTIFACT_METADATA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("metadata sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != ARTIFACT_METADATA_SCHEMA_VERSION
        ):
            raise ValueError(f"unsupported artifact metadata schema version: {self.schema_version}")
        if not isinstance(self.artifact_type, str) or _ID.fullmatch(self.artifact_type) is None:
            raise ValueError("artifact_type must be a lowercase identifier")
        ArtifactReference(f"sha256.{self.sha256}", self.sha256, self.media_type, self.sensitive)
        _timestamp(self.created_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "artifact_type": self.artifact_type,
            "sensitive": self.sensitive,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactMetadata:
        expected = {"schema_version", "sha256", "size_bytes", "media_type", "artifact_type", "sensitive", "created_at"}
        if not isinstance(value, Mapping):
            raise ValueError("artifact metadata must be an object")
        missing = expected - set(value)
        unknown = set(value) - expected
        if missing or unknown:
            details = []
            if missing:
                details.append(f"missing: {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unknown: {', '.join(sorted(unknown))}")
            raise ValueError("invalid artifact metadata fields (" + "; ".join(details) + ")")
        return cls(
            sha256=_string(value["sha256"], "sha256"),
            size_bytes=_integer(value["size_bytes"], "size_bytes"),
            media_type=_string(value["media_type"], "media_type"),
            artifact_type=_string(value["artifact_type"], "artifact_type"),
            sensitive=_boolean(value["sensitive"], "sensitive"),
            created_at=_string(value["created_at"], "created_at"),
            schema_version=_integer(value["schema_version"], "schema_version"),
        )

    def to_reference(self) -> ArtifactReference:
        return ArtifactReference(f"sha256.{self.sha256}", self.sha256, self.media_type, self.sensitive)


@dataclass(frozen=True)
class IntegrityIssue:
    sha256: str
    reason: str


@runtime_checkable
class ArtifactStore(Protocol):
    def put(
        self,
        payload: bytes,
        *,
        media_type: str,
        artifact_type: str = "binary.payload",
        sensitive: bool = False,
    ) -> ArtifactReference: ...

    def read(self, reference: ArtifactReference) -> bytes: ...

    def verify(self, reference: ArtifactReference) -> ArtifactMetadata: ...

    def find_orphans(self, references: Iterable[ArtifactReference]) -> tuple[str, ...]: ...

    def check_integrity(self, references: Iterable[ArtifactReference] | None = None) -> tuple[IntegrityIssue, ...]: ...


class FileSystemArtifactStore:
    """Immutable SHA-256 object store with atomically published object directories."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(
        self,
        payload: bytes,
        *,
        media_type: str,
        artifact_type: str = "binary.payload",
        sensitive: bool = False,
    ) -> ArtifactReference:
        if not isinstance(payload, bytes):
            raise TypeError("artifact payload must be bytes")
        digest = hashlib.sha256(payload).hexdigest()
        reference = ArtifactReference(f"sha256.{digest}", digest, media_type, sensitive)
        destination = self._object_path(digest)
        if destination.exists():
            _, metadata = self._load(reference)
            if metadata.artifact_type != artifact_type:
                raise ArtifactIntegrityError("reference artifact_type does not match stored metadata")
            return reference

        shard = destination.parent
        shard.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=shard))
        try:
            metadata = ArtifactMetadata(
                sha256=digest,
                size_bytes=len(payload),
                media_type=media_type,
                artifact_type=artifact_type,
                sensitive=sensitive,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._write_staged_file(staging / "payload", payload)
            self._write_staged_file(
                staging / "metadata.json",
                json.dumps(metadata.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
            self._fsync_directory(staging)
            try:
                os.rename(staging, destination)
            except OSError:
                if not destination.exists():
                    raise
                _, existing = self._load(reference)
                if existing.artifact_type != artifact_type:
                    raise ArtifactIntegrityError("reference artifact_type does not match stored metadata")
            else:
                self._fsync_directory(shard)
            return reference
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def read(self, reference: ArtifactReference) -> bytes:
        payload, _ = self._load(reference)
        return payload

    def verify(self, reference: ArtifactReference) -> ArtifactMetadata:
        _, metadata = self._load(reference)
        return metadata

    def find_orphans(self, references: Iterable[ArtifactReference]) -> tuple[str, ...]:
        referenced = {item.sha256 for item in references}
        return tuple(sorted(digest for digest in self._iter_digests() if digest not in referenced))

    def check_integrity(self, references: Iterable[ArtifactReference] | None = None) -> tuple[IntegrityIssue, ...]:
        if references is not None:
            issues = []
            for reference in references:
                try:
                    self._load(reference)
                except (ArtifactIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
                    issues.append(IntegrityIssue(reference.sha256, str(exc)))
            return tuple(issues)

        issues = []
        for digest in self._iter_digests():
            try:
                metadata = self._read_metadata(digest)
                if metadata.sha256 != digest:
                    raise ArtifactIntegrityError("metadata digest does not match object path")
                reference = metadata.to_reference()
                self._load(reference)
            except (ArtifactIntegrityError, OSError, ValueError, json.JSONDecodeError) as exc:
                issues.append(IntegrityIssue(digest, str(exc)))
        return tuple(issues)

    def _load(self, reference: ArtifactReference) -> tuple[bytes, ArtifactMetadata]:
        if not isinstance(reference, ArtifactReference):
            raise TypeError("reference must be an ArtifactReference")
        object_path = self._object_path(reference.sha256)
        if not object_path.is_dir() or object_path.is_symlink():
            raise ArtifactIntegrityError(f"artifact not found: {reference.sha256}")
        metadata = self._read_metadata(reference.sha256)
        if metadata.sha256 != reference.sha256:
            raise ArtifactIntegrityError("metadata digest does not match reference")
        if metadata.media_type != reference.media_type or metadata.sensitive != reference.sensitive:
            raise ArtifactIntegrityError("reference metadata does not match stored metadata")
        payload_path = object_path / "payload"
        if not payload_path.is_file() or payload_path.is_symlink():
            raise ArtifactIntegrityError("artifact payload is missing or not a regular file")
        payload = payload_path.read_bytes()
        if len(payload) != metadata.size_bytes:
            raise ArtifactIntegrityError("artifact size does not match stored metadata")
        if hashlib.sha256(payload).hexdigest() != reference.sha256:
            raise ArtifactIntegrityError("artifact content hash mismatch")
        return payload, metadata

    def _read_metadata(self, digest: str) -> ArtifactMetadata:
        path = self._object_path(digest) / "metadata.json"
        if path.is_symlink() or not path.is_file():
            raise ArtifactIntegrityError(f"artifact metadata is missing or not a regular file: {digest}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return ArtifactMetadata.from_dict(raw)
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError(f"artifact metadata is missing: {digest}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ArtifactIntegrityError(f"artifact metadata is invalid: {digest}: {exc}") from exc

    def _object_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise ValueError("digest must be a lowercase SHA-256 hex value")
        return self.root / digest[:2] / digest

    def _iter_digests(self) -> Iterator[str]:
        if not self.root.exists():
            return
        for shard in self.root.iterdir():
            if shard.is_symlink() or not shard.is_dir() or re.fullmatch(r"[0-9a-f]{2}", shard.name) is None:
                continue
            for entry in shard.iterdir():
                if (
                    not entry.is_symlink()
                    and entry.is_dir()
                    and _SHA256.fullmatch(entry.name) is not None
                    and entry.name.startswith(shard.name)
                ):
                    yield entry.name

    @staticmethod
    def _write_staged_file(path: Path, content: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("created_at must be a timezone-qualified ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
