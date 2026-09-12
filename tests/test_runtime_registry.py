import unittest
from dataclasses import dataclass

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


class RuntimeRegistryTest(unittest.TestCase):
    def test_resolves_backend_by_capability(self) -> None:
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

        self.assertEqual("native", backend.descriptor.name)

    def test_skips_unhealthy_backend(self) -> None:
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

        with self.assertRaises(LookupError):
            registry.resolve({RuntimeCapability.INPUT})

    def test_rejects_duplicate_names(self) -> None:
        registry = RuntimeRegistry()
        backend = FakeBackend(RuntimeDescriptor("python", "python"))
        registry.register(backend)

        with self.assertRaises(ValueError):
            registry.register(backend)


if __name__ == "__main__":
    unittest.main()
