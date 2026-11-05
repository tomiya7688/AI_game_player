import dataclasses
import importlib.util
import json
import re
import unittest
from pathlib import Path

from plugin_capability_vectors import manifest_outcome

from ai_game_player.hook_registration import (
    HOOK_MAX_INJECTED_PER_HOOK,
    HOOK_MAX_TIMEOUT_NS,
    HookMode,
)
from ai_game_player.plugin_capability_manifest import (
    FORBIDDEN_PERMISSIONS,
    HOOK_PERMISSIONS,
    ISOLATION_REQUIRED_PERMISSIONS,
    PLUGIN_MANIFEST_FIELDS,
    PLUGIN_MANIFEST_MAX_DATA_CLASSES,
    PLUGIN_MANIFEST_MAX_DESCRIPTION_LENGTH,
    PLUGIN_MANIFEST_MAX_ENCODED_BYTES,
    PLUGIN_MANIFEST_MAX_HOOKS,
    PLUGIN_MANIFEST_MAX_INJECT_TARGETS,
    PLUGIN_MANIFEST_MAX_PAYLOAD_SCHEMAS,
    PLUGIN_MANIFEST_MAX_PERMISSIONS,
    PLUGIN_MANIFEST_MAX_PUBLISHER_LENGTH,
    PLUGIN_MANIFEST_MAX_VERSIONS,
    PLUGIN_MANIFEST_VERSION,
    PluginCapabilityManifest,
    PluginManifestError,
    decode_plugin_capability_manifest,
)
from ai_game_player.stage_envelope import (
    STAGE_ENVELOPE_MAX_DEPTH,
    STAGE_ENVELOPE_MAX_ENCODED_BYTES,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
CONTRACT_DIR = REPOSITORY_ROOT / "schemas" / "plugin_capability_manifest" / "v1"
SCHEMA_PATH = CONTRACT_DIR / "plugin_capability_manifest.schema.json"
CONFORMANCE_PATH = CONTRACT_DIR / "conformance.json"
EXAMPLE_PATH = CONTRACT_DIR / "examples" / "perception_ocr_hint.json"
HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
MANIFEST_ERROR_CODES = {
    PluginManifestError.MALFORMED,
    PluginManifestError.UNSUPPORTED_VERSION,
    PluginManifestError.LIMIT_EXCEEDED,
    PluginManifestError.UNKNOWN_PERMISSION,
    PluginManifestError.FORBIDDEN_PERMISSION,
    PluginManifestError.UNDECLARED_PERMISSION,
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def example() -> dict:
    return load_json(EXAMPLE_PATH)


class PluginManifestConformanceTest(unittest.TestCase):
    def test_reference_validator_matches_every_conformance_vector(self):
        cases = load_json(CONFORMANCE_PATH)["cases"]
        self.assertGreaterEqual(len(cases), 60)
        self.assertEqual(len(cases), len({case["name"] for case in cases}))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(case["expect"], manifest_outcome(case))

    def test_vectors_cover_every_validation_code_and_valid_documents(self):
        expectations = {case["expect"] for case in load_json(CONFORMANCE_PATH)["cases"]}

        self.assertEqual(MANIFEST_ERROR_CODES | {"valid"}, expectations)

    def test_example_round_trips_and_has_a_stable_digest(self):
        manifest = decode_plugin_capability_manifest(EXAMPLE_PATH.read_bytes())
        restored = PluginCapabilityManifest.from_dict(manifest.to_dict())

        self.assertEqual(manifest, restored)
        self.assertEqual(example(), manifest.to_dict())
        self.assertEqual(manifest.digest(), restored.digest())
        self.assertRegex(manifest.digest(), r"^[0-9a-f]{64}$")
        changed = example()
        changed["audit"]["description"] = "changed"
        self.assertNotEqual(
            manifest.digest(), PluginCapabilityManifest.from_dict(changed).digest()
        )

    def test_normalizes_order_of_permissions_versions_hooks_and_classes(self):
        document = example()
        document["permissions"].reverse()
        document["hooks"].reverse()
        document["hooks"][0]["data_classes"] = ["z_class", "a_class"]
        document["compatibility"]["stage_envelope_schema_versions"] = [3, 1]

        manifest = PluginCapabilityManifest.from_dict(document)

        self.assertEqual(("hook.inject", "hook.read"), manifest.permissions)
        self.assertEqual(
            [
                "plugin.example.ocr_hint.hinter",
                "plugin.example.ocr_hint.observer",
            ],
            [hook.hook_id for hook in manifest.hooks],
        )
        self.assertEqual((1, 3), manifest.stage_envelope_schema_versions)
        self.assertEqual(
            ("a_class", "z_class"),
            manifest.hook("plugin.example.ocr_hint.observer").data_classes,
        )
        self.assertIsNone(manifest.hook("plugin.example.ocr_hint.missing"))


class PluginManifestSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = load_json(SCHEMA_PATH)

    def test_schema_fields_and_limits_match_reference_constants(self):
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema", self.schema["$schema"]
        )
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(list(PLUGIN_MANIFEST_FIELDS), self.schema["required"])
        self.assertEqual(
            PLUGIN_MANIFEST_VERSION,
            self.schema["properties"]["manifest_version"]["const"],
        )
        self.assertEqual(
            {
                "max_encoded_bytes": PLUGIN_MANIFEST_MAX_ENCODED_BYTES,
                "max_depth": STAGE_ENVELOPE_MAX_DEPTH,
                "max_hooks": PLUGIN_MANIFEST_MAX_HOOKS,
                "max_permissions": PLUGIN_MANIFEST_MAX_PERMISSIONS,
                "max_payload_schemas": PLUGIN_MANIFEST_MAX_PAYLOAD_SCHEMAS,
                "max_inject_targets": PLUGIN_MANIFEST_MAX_INJECT_TARGETS,
                "max_versions": PLUGIN_MANIFEST_MAX_VERSIONS,
                "max_data_classes": PLUGIN_MANIFEST_MAX_DATA_CLASSES,
                "max_publisher_length": PLUGIN_MANIFEST_MAX_PUBLISHER_LENGTH,
                "max_description_length": PLUGIN_MANIFEST_MAX_DESCRIPTION_LENGTH,
            },
            self.schema["x-kadoka-limits"],
        )
        budget = self.schema["$defs"]["hook"]["properties"]["budget"]["properties"]
        self.assertEqual(HOOK_MAX_TIMEOUT_NS, budget["timeout_ns"]["maximum"])
        self.assertEqual(
            STAGE_ENVELOPE_MAX_ENCODED_BYTES, budget["max_output_bytes"]["maximum"]
        )
        self.assertEqual(HOOK_MAX_INJECTED_PER_HOOK, budget["max_injected"]["maximum"])

    def test_permission_registry_matches_schema(self):
        registry = self.schema["x-kadoka-permissions"]

        self.assertEqual(sorted(HOOK_PERMISSIONS), registry["in_process"])
        self.assertEqual(
            sorted(ISOLATION_REQUIRED_PERMISSIONS), registry["isolation_required"]
        )
        self.assertEqual(sorted(FORBIDDEN_PERMISSIONS), registry["forbidden"])
        self.assertEqual(
            HOOK_PERMISSIONS | ISOLATION_REQUIRED_PERMISSIONS,
            set(self.schema["properties"]["permissions"]["items"]["enum"]),
        )
        self.assertEqual(
            {mode.value for mode in HookMode},
            set(self.schema["$defs"]["hook"]["properties"]["mode"]["enum"]),
        )

    def test_writable_stage_pattern_excludes_safety_and_execution(self):
        pattern = self.schema["$defs"]["writable_stage"]["pattern"]
        for stage in ("perception", "decision", "ext.plugin.x"):
            self.assertIsNotNone(re.search(pattern, stage), stage)
        for stage in ("safety", "execution", "perception\n", "ext."):
            self.assertIsNone(re.search(pattern, stage), stage)

    @unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is a development dependency")
    def test_schema_alone_agrees_with_schema_valid_flags(self):
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(self.schema)
        validator = Draft202012Validator(self.schema)
        validator.validate(example())
        for case in load_json(CONFORMANCE_PATH)["cases"]:
            if case["schema_valid"] is None:
                continue
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    case["schema_valid"], validator.is_valid(case["document"])
                )


class PluginManifestValidationTest(unittest.TestCase):
    def assert_code(self, code, document):
        with self.assertRaises(PluginManifestError) as raised:
            PluginCapabilityManifest.from_dict(document)
        self.assertEqual(code, raised.exception.code)

    def test_versions_newer_or_older_than_v1_are_unsupported(self):
        for version in (0, 2, 999):
            with self.subTest(version=version):
                document = example()
                document["manifest_version"] = version
                self.assert_code(PluginManifestError.UNSUPPORTED_VERSION, document)

    def test_first_violation_in_field_order_decides_the_code(self):
        document = example()
        document["plugin_id"] = "Bad"
        document["permissions"] = ["input.direct"]

        self.assert_code(PluginManifestError.MALFORMED, document)
        document["plugin_id"] = "plugin.example.ocr_hint"
        self.assert_code(PluginManifestError.FORBIDDEN_PERMISSION, document)

    def test_rejects_non_bytes_input_and_oversized_strings_before_parsing(self):
        for data in (None, 42, ["{}"]):
            with self.subTest(data=data):
                with self.assertRaises(PluginManifestError) as raised:
                    decode_plugin_capability_manifest(data)
                self.assertEqual(PluginManifestError.MALFORMED, raised.exception.code)
        multibyte = '"' + "あ" * 30_000 + '"'
        with self.assertRaises(PluginManifestError) as raised:
            decode_plugin_capability_manifest(multibyte)
        self.assertEqual(PluginManifestError.LIMIT_EXCEEDED, raised.exception.code)

    def test_manifest_is_immutable(self):
        manifest = PluginCapabilityManifest.from_dict(example())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.plugin_id = "plugin.other"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
