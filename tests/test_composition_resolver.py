import unittest

from ai_game_player.runtime import CapabilityManifest, CapabilityManifestRegistry, CompositionResolver


def manifest(manifest_id: str, **kwargs) -> CapabilityManifest:
    return CapabilityManifest(
        manifest_id=manifest_id,
        capabilities=frozenset({"ocr.text"}),
        config_namespace=f"features.{manifest_id}",
        **kwargs,
    )


class FakeProvider:
    def __init__(self, *, healthy: bool = True, fail_initialize: bool = False) -> None:
        self.healthy = healthy
        self.fail_initialize = fail_initialize
        self.initialized = False
        self.closed = 0

    def initialize(self) -> None:
        self.initialized = True
        if self.fail_initialize:
            raise RuntimeError("startup failed")

    def is_healthy(self) -> bool:
        return self.healthy

    def close(self) -> None:
        self.closed += 1


class CompositionResolverTest(unittest.TestCase):
    def registry(self, *items: CapabilityManifest) -> CapabilityManifestRegistry:
        registry = CapabilityManifestRegistry()
        for item in items:
            registry.register(item)
        registry.validate()
        return registry

    def test_selects_preferred_provider_and_exposes_lifecycle(self) -> None:
        registry = self.registry(manifest("ocr.basic"), manifest("ocr.fast"))
        basic = FakeProvider()
        fast = FakeProvider()
        resolver = CompositionResolver(
            registry,
            {"ocr.basic": lambda: basic, "ocr.fast": lambda: fast},
            preferred={"ocr.text": "ocr.fast"},
            available_dependencies=set(),
        )

        result = resolver.resolve("ocr.text")

        self.assertEqual("ocr.fast", result.manifest.manifest_id)
        self.assertFalse(result.degraded)
        self.assertTrue(fast.initialized)
        result.lifecycle.close()
        result.lifecycle.close()
        self.assertEqual(1, fast.closed)
        self.assertEqual(0, basic.initialized)

    def test_initialization_failure_uses_fallback_and_reports_reason(self) -> None:
        registry = self.registry(manifest("ocr.basic"), manifest("ocr.fast", fallback_manifest_id="ocr.basic"))
        broken = FakeProvider(fail_initialize=True)
        fallback = FakeProvider()
        resolver = CompositionResolver(
            registry,
            {"ocr.fast": lambda: broken, "ocr.basic": lambda: fallback},
            available_dependencies=set(),
            preferred={"ocr.text": "ocr.fast"},
        )

        result = resolver.resolve("ocr.text")

        self.assertEqual("ocr.basic", result.manifest.manifest_id)
        self.assertTrue(result.degraded)
        self.assertIn("initialization failed: startup failed", result.reasons[0].reason)
        self.assertEqual(1, broken.closed)

    def test_missing_required_dependency_skips_provider_for_fallback(self) -> None:
        registry = self.registry(
            manifest("ocr.basic"),
            manifest(
                "ocr.fast",
                required_dependencies=frozenset({"onnxruntime"}),
                fallback_manifest_id="ocr.basic",
            ),
        )
        fallback = FakeProvider()
        resolver = CompositionResolver(
            registry,
            {"ocr.basic": lambda: fallback, "ocr.fast": lambda: self.fail("must not initialize")},
            available_dependencies=set(),
            preferred={"ocr.text": "ocr.fast"},
        )

        result = resolver.resolve("ocr.text")

        self.assertEqual("ocr.basic", result.manifest.manifest_id)
        self.assertIn("missing required dependencies", result.reasons[0].reason)

    def test_optional_dependency_absence_returns_degraded_reason(self) -> None:
        registry = self.registry(
            manifest(
                "ocr.optional",
                optional_dependencies=frozenset({"language-data"}),
                degraded_mode="without-language-data",
            )
        )
        provider = FakeProvider()
        resolver = CompositionResolver(
            registry, {"ocr.optional": lambda: provider}, available_dependencies=set()
        )

        result = resolver.resolve("ocr.text")

        self.assertTrue(result.degraded)
        self.assertTrue(provider.initialized)
        self.assertIn("degraded mode: without-language-data", result.reasons[0].reason)

    def test_optional_dependency_unknown_is_degraded_not_unavailable(self) -> None:
        registry = self.registry(
            manifest(
                "ocr.optional",
                optional_dependencies=frozenset({"language-data"}),
                degraded_mode="without-language-data",
            )
        )
        provider = FakeProvider()
        result = CompositionResolver(registry, {"ocr.optional": lambda: provider}).resolve("ocr.text")

        self.assertTrue(result.degraded)
        self.assertIn("availability is unknown", result.reasons[0].reason)

    def test_unavailable_capability_has_actionable_reason(self) -> None:
        resolver = CompositionResolver(CapabilityManifestRegistry(), {})
        with self.assertRaisesRegex(LookupError, "capability unavailable: vision.scene"):
            resolver.resolve("vision.scene")

    def test_context_manager_closes_provider_on_exit(self) -> None:
        registry = self.registry(manifest("ocr.basic"))
        provider = FakeProvider()
        result = CompositionResolver(
            registry, {"ocr.basic": lambda: provider}, available_dependencies=set()
        ).resolve("ocr.text")

        with result.lifecycle:
            self.assertFalse(result.lifecycle.closed)

        self.assertTrue(result.lifecycle.closed)
        self.assertEqual(1, provider.closed)


if __name__ == "__main__":
    unittest.main()
