from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ai_game_player.runtime.contracts import RUNTIME_CONTRACT_VERSION


MANIFEST_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]*$")


class FeatureLifecycle(str, Enum):
    EXPERIMENTAL = "experimental"
    BETA = "beta"
    STABLE = "stable"
    DEPRECATED = "deprecated"


@dataclass(frozen=True)
class CapabilityManifest:
    manifest_id: str
    capabilities: frozenset[str]
    config_namespace: str
    schema_version: int = MANIFEST_SCHEMA_VERSION
    contract_version: int = RUNTIME_CONTRACT_VERSION
    required_dependencies: frozenset[str] = field(default_factory=frozenset)
    optional_dependencies: frozenset[str] = field(default_factory=frozenset)
    lifecycle: FeatureLifecycle = FeatureLifecycle.EXPERIMENTAL
    fallback_manifest_id: str | None = None
    degraded_mode: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.manifest_id, "manifest_id")
        _validate_identifier(self.config_namespace, "config_namespace")
        for value in self.capabilities:
            _validate_identifier(value, "capability")
        for value in self.required_dependencies | self.optional_dependencies:
            _validate_identifier(value, "dependency")
        if not self.capabilities:
            raise ValueError("manifest must provide at least one capability")
        if self.required_dependencies & self.optional_dependencies:
            raise ValueError("a dependency cannot be both required and optional")
        if self.manifest_id in self.required_dependencies | self.optional_dependencies:
            raise ValueError("manifest cannot depend on itself")
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema version: {self.schema_version}")
        if self.contract_version != RUNTIME_CONTRACT_VERSION:
            raise ValueError(f"incompatible runtime contract version: {self.contract_version}")
        if self.fallback_manifest_id is not None:
            _validate_identifier(self.fallback_manifest_id, "fallback_manifest_id")
            if self.fallback_manifest_id == self.manifest_id:
                raise ValueError("manifest cannot fall back to itself")
        if self.optional_dependencies and not self.degraded_mode:
            raise ValueError("optional dependencies require an explicit degraded_mode")
        if self.degraded_mode is not None:
            _validate_identifier(self.degraded_mode, "degraded_mode")
        if not isinstance(self.lifecycle, FeatureLifecycle):
            try:
                object.__setattr__(self, "lifecycle", FeatureLifecycle(self.lifecycle))
            except ValueError as exc:
                raise ValueError(f"unsupported feature lifecycle: {self.lifecycle}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "contract_version": self.contract_version,
            "capabilities": sorted(self.capabilities),
            "required_dependencies": sorted(self.required_dependencies),
            "optional_dependencies": sorted(self.optional_dependencies),
            "lifecycle": self.lifecycle.value,
            "fallback_manifest_id": self.fallback_manifest_id,
            "degraded_mode": self.degraded_mode,
            "config_namespace": self.config_namespace,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityManifest:
        allowed = {
            "schema_version",
            "manifest_id",
            "contract_version",
            "capabilities",
            "required_dependencies",
            "optional_dependencies",
            "lifecycle",
            "fallback_manifest_id",
            "degraded_mode",
            "config_namespace",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown manifest fields: {', '.join(sorted(unknown))}")
        required_fields = {"schema_version", "manifest_id", "contract_version", "capabilities", "config_namespace"}
        missing = required_fields - set(value)
        if missing:
            raise ValueError(f"missing manifest fields: {', '.join(sorted(missing))}")
        return cls(
            manifest_id=_require_string(value["manifest_id"], "manifest_id"),
            capabilities=_string_set(value["capabilities"], "capabilities"),
            config_namespace=_require_string(value["config_namespace"], "config_namespace"),
            schema_version=_require_int(value["schema_version"], "schema_version"),
            contract_version=_require_int(value["contract_version"], "contract_version"),
            required_dependencies=_string_set(value.get("required_dependencies", []), "required_dependencies"),
            optional_dependencies=_string_set(value.get("optional_dependencies", []), "optional_dependencies"),
            lifecycle=FeatureLifecycle(_require_string(value.get("lifecycle", "experimental"), "lifecycle")),
            fallback_manifest_id=_optional_string(value.get("fallback_manifest_id"), "fallback_manifest_id"),
            degraded_mode=_optional_string(value.get("degraded_mode"), "degraded_mode"),
        )


class CapabilityManifestRegistry:
    """Stores and validates optional feature declarations without selecting providers."""

    def __init__(self) -> None:
        self._manifests: dict[str, CapabilityManifest] = {}

    def register(self, manifest: CapabilityManifest) -> None:
        if manifest.manifest_id in self._manifests:
            raise ValueError(f"capability manifest already registered: {manifest.manifest_id}")
        self._manifests[manifest.manifest_id] = manifest

    def get(self, manifest_id: str) -> CapabilityManifest:
        try:
            return self._manifests[manifest_id]
        except KeyError as exc:
            raise KeyError(f"capability manifest not found: {manifest_id}") from exc

    def validate(self, available_dependencies: Iterable[str] | None = None) -> None:
        available = None if available_dependencies is None else frozenset(available_dependencies)
        for manifest in self._manifests.values():
            fallback_id = manifest.fallback_manifest_id
            if fallback_id is not None and fallback_id not in self._manifests:
                raise ValueError(f"fallback manifest not registered: {fallback_id}")
        for manifest in self._manifests.values():
            fallback_id = manifest.fallback_manifest_id
            if fallback_id is None:
                continue
            fallback = self._manifests[fallback_id]
            if not manifest.capabilities.issubset(fallback.capabilities):
                raise ValueError(f"fallback manifest {fallback_id} does not provide all requested capabilities")
            self._fallback_chain(manifest.manifest_id)
        if available is not None:
            for manifest in self._manifests.values():
                missing = manifest.required_dependencies - available
                if missing and manifest.fallback_manifest_id is None:
                    raise ValueError(
                        f"manifest {manifest.manifest_id} is missing required dependencies: "
                        f"{', '.join(sorted(missing))}"
                    )

    def manifests_for(self, capability: str) -> tuple[CapabilityManifest, ...]:
        _validate_identifier(capability, "capability")
        return tuple(manifest for manifest in self._manifests.values() if capability in manifest.capabilities)

    def _fallback_chain(self, manifest_id: str) -> None:
        visited: set[str] = set()
        current_id: str | None = manifest_id
        while current_id is not None:
            if current_id in visited:
                raise ValueError(f"fallback cycle detected at manifest: {current_id}")
            visited.add(current_id)
            current = self._manifests[current_id]
            current_id = current.fallback_manifest_id


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase identifier")


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    return None if value is None else _require_string(value, field_name)


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _string_set(value: Any, field_name: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} must not contain duplicates")
    return frozenset(value)
