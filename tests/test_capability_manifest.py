import json
import unittest
from pathlib import Path

from ai_game_player.runtime import (
    CapabilityManifest,
    CapabilityManifestRegistry,
    FeatureLifecycle,
    RUNTIME_CONTRACT_VERSION,
)


def manifest(
    manifest_id: str,
    capabilities: frozenset[str],
    **kwargs,
) -> CapabilityManifest:
    return CapabilityManifest(
        manifest_id=manifest_id,
        capabilities=capabilities,
        config_namespace=f"features.{manifest_id}",
        **kwargs,
    )


class CapabilityManifestTest(unittest.TestCase):
    def test_manifest_round_trips_through_machine_readable_dict(self):
        original = manifest(
            "ocr.tesseract",
            frozenset({"ocr.text"}),
            required_dependencies=frozenset({"pillow"}),
            optional_dependencies=frozenset({"tesseract"}),
            lifecycle=FeatureLifecycle.BETA,
            degraded_mode="ocr-without-language-data",
        )

        self.assertEqual(original, CapabilityManifest.from_dict(original.to_dict()))

    def test_manifest_schema_is_valid_json_schema_document(self):
        path = Path(__file__).parents[1] / "config" / "capability_manifest.schema.json"

        schema = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual("object", schema["type"])
        self.assertIn("contract_version", schema["required"])
        self.assertIn("capabilities", schema["properties"])

    def test_rejects_incompatible_runtime_contract_version(self):
        with self.assertRaisesRegex(ValueError, "incompatible runtime contract"):
            manifest("feature.test", frozenset({"capture.window"}), contract_version=RUNTIME_CONTRACT_VERSION + 1)

    def test_rejects_overlapping_required_and_optional_dependencies(self):
        with self.assertRaisesRegex(ValueError, "both required and optional"):
            manifest(
                "feature.test",
                frozenset({"capture.window"}),
                required_dependencies=frozenset({"pillow"}),
                optional_dependencies=frozenset({"pillow"}),
                degraded_mode="basic-mode",
            )

    def test_optional_dependency_requires_explicit_degraded_mode(self):
        with self.assertRaisesRegex(ValueError, "explicit degraded_mode"):
            manifest("feature.test", frozenset({"ocr.text"}), optional_dependencies=frozenset({"tesseract"}))

    def test_optional_dependencies_and_degraded_mode_are_declared_without_selection(self):
        registry = CapabilityManifestRegistry()
        optional = manifest(
            "ocr.optional",
            frozenset({"ocr.text"}),
            optional_dependencies=frozenset({"language-data"}),
            degraded_mode="ocr-without-language-data",
        )
        registry.register(optional)

        registry.validate(available_dependencies=set())

        self.assertEqual(optional, registry.manifests_for("ocr.text")[0])

    def test_required_dependency_and_fallback_are_declared_and_validated(self):
        registry = CapabilityManifestRegistry()
        basic = manifest("ocr.basic", frozenset({"ocr.text", "ocr.layout"}), lifecycle=FeatureLifecycle.STABLE)
        registry.register(
            manifest(
                "ocr.advanced",
                frozenset({"ocr.text", "ocr.layout"}),
                required_dependencies=frozenset({"onnxruntime"}),
                fallback_manifest_id="ocr.basic",
            )
        )
        registry.register(basic)

        registry.validate(available_dependencies=set())

        self.assertEqual("ocr.basic", registry.get("ocr.advanced").fallback_manifest_id)
        self.assertIn(basic, registry.manifests_for("ocr.layout"))

    def test_rejects_fallback_that_does_not_cover_capabilities(self):
        registry = CapabilityManifestRegistry()
        registry.register(manifest("ocr.basic", frozenset({"ocr.text"})))
        registry.register(
            manifest(
                "ocr.advanced",
                frozenset({"ocr.text", "ocr.layout"}),
                fallback_manifest_id="ocr.basic",
            )
        )

        with self.assertRaisesRegex(ValueError, "does not provide all"):
            registry.validate()

    def test_discovers_all_manifests_for_a_capability_without_selection(self):
        registry = CapabilityManifestRegistry()
        registry.register(
            manifest(
                "ocr.optional",
                frozenset({"ocr.text"}),
                required_dependencies=frozenset({"missing-package"}),
            )
        )
        registry.register(manifest("ocr.basic", frozenset({"ocr.text"})))

        manifests = registry.manifests_for("ocr.text")

        self.assertEqual(("ocr.optional", "ocr.basic"), tuple(item.manifest_id for item in manifests))

    def test_rejects_missing_required_dependency_without_fallback(self):
        registry = CapabilityManifestRegistry()
        registry.register(
            manifest("ocr.required", frozenset({"ocr.text"}), required_dependencies=frozenset({"pillow"}))
        )

        with self.assertRaisesRegex(ValueError, "missing required dependencies: pillow"):
            registry.validate(available_dependencies=set())

    def test_rejects_unknown_manifest_fields_and_schema_versions(self):
        value = manifest("feature.test", frozenset({"capture.window"})).to_dict()
        with_unknown = dict(value, typo_field=True)
        with_bad_version = dict(value, schema_version=999)

        with self.assertRaisesRegex(ValueError, "unknown manifest fields"):
            CapabilityManifest.from_dict(with_unknown)
        with self.assertRaisesRegex(ValueError, "unsupported manifest schema"):
            CapabilityManifest.from_dict(with_bad_version)

    def test_rejects_missing_fallback_reference_and_cycles(self):
        missing_registry = CapabilityManifestRegistry()
        missing_registry.register(
            manifest("feature.primary", frozenset({"feature.run"}), fallback_manifest_id="feature.missing")
        )
        with self.assertRaisesRegex(ValueError, "fallback manifest not registered"):
            missing_registry.validate()

        cyclic_registry = CapabilityManifestRegistry()
        cyclic_registry.register(
            manifest("feature.first", frozenset({"feature.run"}), fallback_manifest_id="feature.second")
        )
        cyclic_registry.register(
            manifest("feature.second", frozenset({"feature.run"}), fallback_manifest_id="feature.first")
        )
        with self.assertRaisesRegex(ValueError, "fallback cycle detected"):
            cyclic_registry.validate()


if __name__ == "__main__":
    unittest.main()
