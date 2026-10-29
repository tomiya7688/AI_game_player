from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ai_game_player.runtime.manifest import CapabilityManifest, CapabilityManifestRegistry


@dataclass(frozen=True)
class ResolutionFailure:
    manifest_id: str
    reason: str


class LifecycleHandle:
    """Owns one initialized provider and releases it exactly once."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _close_provider(self._provider)

    def __enter__(self) -> LifecycleHandle:
        if self._closed:
            raise RuntimeError("lifecycle handle is already closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


@dataclass(frozen=True)
class ResolvedFeature:
    capability: str
    manifest: CapabilityManifest
    provider: Any
    degraded: bool
    reasons: tuple[ResolutionFailure, ...]
    lifecycle: LifecycleHandle


class CompositionResolver:
    """Selects and initializes a provider for one declared capability."""

    def __init__(
        self,
        manifests: CapabilityManifestRegistry,
        factories: Mapping[str, Callable[[], Any]],
        *,
        available_dependencies: set[str] | frozenset[str] | None = None,
        preferred: Mapping[str, str] | None = None,
    ) -> None:
        self._manifests = manifests
        self._factories = dict(factories)
        self._available_dependencies = (
            None if available_dependencies is None else frozenset(available_dependencies)
        )
        self._preferred = dict(preferred or {})

    def resolve(self, capability: str, *, preferred_manifest_id: str | None = None) -> ResolvedFeature:
        candidates = self._manifests.manifests_for(capability)
        if not candidates:
            raise LookupError(f"capability unavailable: {capability}")

        preferred_id = preferred_manifest_id or self._preferred.get(capability)
        if preferred_id is not None:
            preferred_manifest = self._manifests.get(preferred_id)
            if capability not in preferred_manifest.capabilities:
                raise ValueError(f"preferred manifest {preferred_id} does not provide capability {capability}")
            candidates = (preferred_manifest,) + tuple(item for item in candidates if item.manifest_id != preferred_id)

        failures: list[ResolutionFailure] = []
        attempted: set[str] = set()
        for candidate in candidates:
            resolved = self._try_chain(capability, candidate, failures, attempted)
            if resolved is not None:
                return resolved

        reasons = "; ".join(f"{item.manifest_id}: {item.reason}" for item in failures)
        raise LookupError(f"no provider available for capability {capability}: {reasons or 'no usable provider'}")

    def _try_chain(
        self,
        capability: str,
        manifest: CapabilityManifest,
        failures: list[ResolutionFailure],
        attempted: set[str],
    ) -> ResolvedFeature | None:
        current: CapabilityManifest | None = manifest
        while current is not None:
            if current.manifest_id in attempted:
                break
            attempted.add(current.manifest_id)
            reason = self._unavailable_reason(current)
            if reason is not None:
                failures.append(ResolutionFailure(current.manifest_id, reason))
                current = self._fallback(current)
                continue

            factory = self._factories.get(current.manifest_id)
            if factory is None:
                failures.append(ResolutionFailure(current.manifest_id, "provider factory is not registered"))
                current = self._fallback(current)
                continue

            provider: Any | None = None
            try:
                provider = factory()
                initialize = getattr(provider, "initialize", None)
                if initialize is not None:
                    initialize()
                health_check = getattr(provider, "is_healthy", None)
                if health_check is None:
                    raise RuntimeError("provider does not expose a health check")
                if not health_check():
                    raise RuntimeError("provider health check failed")
            except Exception as exc:
                if provider is not None:
                    try:
                        _close_provider(provider)
                    except Exception as cleanup_exc:
                        failures.append(
                            ResolutionFailure(current.manifest_id, f"cleanup after initialization failure: {cleanup_exc}")
                        )
                failures.append(ResolutionFailure(current.manifest_id, f"initialization failed: {exc}"))
                current = self._fallback(current)
                continue

            missing_optional = (
                current.optional_dependencies - self._available_dependencies
                if self._available_dependencies is not None
                else frozenset()
            )
            degraded = bool(failures) or bool(missing_optional)
            if missing_optional:
                dependency_state = ", ".join(sorted(missing_optional))
                failures.append(
                    ResolutionFailure(
                        current.manifest_id,
                        f"optional dependencies unavailable ({dependency_state}); "
                        f"degraded mode: {current.degraded_mode}",
                    )
                )
            if current.optional_dependencies and self._available_dependencies is None:
                degraded = True
                failures.append(ResolutionFailure(current.manifest_id, "optional dependency availability is unknown"))
            return ResolvedFeature(
                capability=capability,
                manifest=current,
                provider=provider,
                degraded=degraded,
                reasons=tuple(failures),
                lifecycle=LifecycleHandle(provider),
            )
        return None

    def _unavailable_reason(self, manifest: CapabilityManifest) -> str | None:
        if self._available_dependencies is not None:
            missing = manifest.required_dependencies - self._available_dependencies
            if missing:
                return f"missing required dependencies: {', '.join(sorted(missing))}"
        if manifest.manifest_id not in self._factories:
            return "provider factory is not registered"
        return None

    def _fallback(self, manifest: CapabilityManifest) -> CapabilityManifest | None:
        if manifest.fallback_manifest_id is None:
            return None
        return self._manifests.get(manifest.fallback_manifest_id)


def _close_provider(provider: Any) -> None:
    close = getattr(provider, "close", None)
    if close is None:
        close = getattr(provider, "shutdown", None)
    if close is not None:
        close()
