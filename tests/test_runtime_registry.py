from dataclasses import dataclass

import pytest

from ai_game_player.runtime import (
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeRegistry,
)


@dataclass
class FakeBackend:
    descriptor: RuntimeDescriptor
    healthy: bool = True

    def is_healthy(self) -> bool:
        return self.healthy


def test_registry_resolves_backend_by_capability() -> None:
    registry = RuntimeRegistry()
    registry.register(
        FakeBackend(
            RuntimeDescriptor(
                "native",
                "cpp",
                capabilities=frozenset({RuntimeCapability.CAPTURE, RuntimeCapability.SAFETY}),
            )
        )
    )

    backend = registry.resolve({RuntimeCapability.CAPTURE})

    assert backend.descriptor.name == "native"


def test_registry_skips_unhealthy_backend() -> None:
    registry = RuntimeRegistry()
    registry.register(
        FakeBackend(
            RuntimeDescriptor(
                "native",
                "cpp",
                capabilities=frozenset({RuntimeCapability.INPUT}),
            ),
            healthy=False,
        )
    )

    with pytest.raises(LookupError):
        registry.resolve({RuntimeCapability.INPUT})


def test_registry_rejects_duplicate_names() -> None:
    registry = RuntimeRegistry()
    backend = FakeBackend(RuntimeDescriptor("python", "python"))
    registry.register(backend)

    with pytest.raises(ValueError):
        registry.register(backend)
