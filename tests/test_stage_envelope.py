import ast
import copy
import dataclasses
import importlib.util
import json
import re
import unittest
from pathlib import Path

from ai_game_player.experience import ArtifactReference
from ai_game_player.stage_envelope import (
    CORE_STAGE_IDS,
    PRODUCER_KINDS,
    STAGE_BOUNDARIES,
    STAGE_ENVELOPE_FIELDS,
    STAGE_ENVELOPE_INTEGER_MAX,
    STAGE_ENVELOPE_INTEGER_MIN,
    STAGE_ENVELOPE_MAX_ARTIFACT_REFS,
    STAGE_ENVELOPE_MAX_DEPTH,
    STAGE_ENVELOPE_MAX_DERIVED_FROM,
    STAGE_ENVELOPE_MAX_ENCODED_BYTES,
    STAGE_ENVELOPE_SCHEMA_VERSION,
    StageEnvelope,
    StageEnvelopeError,
)
from ai_game_player.stage_envelope_codec import (
    decode_stage_envelope,
    encode_stage_envelope,
    migrate_stage_envelope_document,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
CONTRACT_DIR = REPOSITORY_ROOT / "schemas" / "stage_envelope" / "v1"
SCHEMA_PATH = CONTRACT_DIR / "stage_envelope.schema.json"
CONFORMANCE_PATH = CONTRACT_DIR / "conformance.json"
EXAMPLE_PATHS = sorted((CONTRACT_DIR / "examples").glob("*.json"))
HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def example_document(name: str = "core_perception_output.json") -> dict:
    return load_json(CONTRACT_DIR / "examples" / name)


def make_envelope(**overrides) -> StageEnvelope:
    values = {
        "envelope_id": "env-1",
        "stage": "state",
        "boundary": "output",
        "session_id": "session-1",
        "timestamp_utc": "2026-11-04T00:00:00Z",
        "trace_id": "trace-1",
        "producer_kind": "core",
        "producer_id": "core.state_builder",
        "producer_version": "1",
        "payload_schema": "kadoka.state.snapshot",
        "payload_schema_version": 1,
    }
    values.update(overrides)
    return StageEnvelope(**values)


def nested_object(depth: int) -> dict:
    value: dict = {}
    for _ in range(depth):
        value = {"n": value}
    return value


def validator_outcome(case: dict) -> str:
    data = (
        case["text"]
        if "text" in case
        else json.dumps(case["document"], ensure_ascii=False)
    )
    try:
        decode_stage_envelope(data)
    except StageEnvelopeError as error:
        return error.code
    return "valid"


class StageEnvelopeConformanceTest(unittest.TestCase):
    def test_reference_validator_matches_every_conformance_vector(self):
        cases = load_json(CONFORMANCE_PATH)["cases"]
        self.assertGreaterEqual(len(cases), 30)
        self.assertEqual(len(cases), len({case["name"] for case in cases}))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(case["expect"], validator_outcome(case))

    def test_conformance_vectors_cover_every_error_code_and_valid_documents(self):
        expectations = {case["expect"] for case in load_json(CONFORMANCE_PATH)["cases"]}

        self.assertEqual(
            {
                "valid",
                StageEnvelopeError.MALFORMED,
                StageEnvelopeError.UNSUPPORTED_VERSION,
                StageEnvelopeError.LIMIT_EXCEEDED,
            },
            expectations,
        )

    def test_examples_are_valid_and_round_trip_to_identical_canonical_bytes(self):
        self.assertEqual(2, len(EXAMPLE_PATHS))
        for path in EXAMPLE_PATHS:
            with self.subTest(example=path.name):
                envelope = decode_stage_envelope(path.read_bytes())
                encoded = encode_stage_envelope(envelope)
                restored = decode_stage_envelope(encoded)

                self.assertEqual(envelope, restored)
                self.assertEqual(encoded, encode_stage_envelope(restored))
                self.assertEqual(load_json(path), restored.to_dict())


class StageEnvelopeSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema = load_json(SCHEMA_PATH)

    def test_schema_fields_match_reference_validator(self):
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema", self.schema["$schema"]
        )
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(list(STAGE_ENVELOPE_FIELDS), self.schema["required"])
        self.assertEqual(set(STAGE_ENVELOPE_FIELDS), set(self.schema["properties"]))
        self.assertEqual(
            STAGE_ENVELOPE_SCHEMA_VERSION,
            self.schema["properties"]["schema_version"]["const"],
        )

    def test_schema_limits_annotation_matches_reference_constants(self):
        self.assertEqual(
            {
                "max_encoded_bytes": STAGE_ENVELOPE_MAX_ENCODED_BYTES,
                "max_depth": STAGE_ENVELOPE_MAX_DEPTH,
                "max_derived_from": STAGE_ENVELOPE_MAX_DERIVED_FROM,
                "max_artifact_refs": STAGE_ENVELOPE_MAX_ARTIFACT_REFS,
                "integer_min": STAGE_ENVELOPE_INTEGER_MIN,
                "integer_max": STAGE_ENVELOPE_INTEGER_MAX,
            },
            self.schema["x-kadoka-limits"],
        )
        properties = self.schema["properties"]
        self.assertEqual(
            STAGE_ENVELOPE_MAX_DERIVED_FROM, properties["derived_from"]["maxItems"]
        )
        self.assertEqual(
            STAGE_ENVELOPE_MAX_ARTIFACT_REFS, properties["artifact_refs"]["maxItems"]
        )

    def test_schema_enumerations_match_reference_validator(self):
        properties = self.schema["properties"]
        stage_pattern = properties["stage"]["pattern"]

        self.assertEqual(STAGE_BOUNDARIES, frozenset(properties["boundary"]["enum"]))
        self.assertEqual(PRODUCER_KINDS, frozenset(properties["producer_kind"]["enum"]))
        for stage in CORE_STAGE_IDS | {"ext.plugin.filter"}:
            self.assertIsNotNone(re.search(stage_pattern, stage), stage)
        for stage in (
            "ext.",
            "ext..plugin",
            "ext.1x",
            "render",
            "perception\n",
            "Ext.plugin",
        ):
            self.assertIsNone(re.search(stage_pattern, stage), stage)

    @unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is a development dependency")
    def test_schema_alone_agrees_with_schema_valid_flags(self):
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(self.schema)
        validator = Draft202012Validator(self.schema)
        for path in EXAMPLE_PATHS:
            validator.validate(load_json(path))
        for case in load_json(CONFORMANCE_PATH)["cases"]:
            if case["schema_valid"] is None:
                continue
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    case["schema_valid"], validator.is_valid(case["document"])
                )


class StageEnvelopeValidationTest(unittest.TestCase):
    def test_unknown_versions_are_rejected_as_unsupported(self):
        for version in (0, 2, 999):
            with self.subTest(version=version):
                with self.assertRaises(StageEnvelopeError) as raised:
                    make_envelope(schema_version=version)
                self.assertEqual(
                    StageEnvelopeError.UNSUPPORTED_VERSION, raised.exception.code
                )

    def test_non_json_payload_values_are_rejected(self):
        for payload in (
            {"value": {1, 2}},
            {"value": object()},
            {1: "integer key"},
            {"value": float("nan")},
        ):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(StageEnvelopeError) as raised:
                    make_envelope(payload=payload)
                self.assertEqual(StageEnvelopeError.MALFORMED, raised.exception.code)

    def test_depth_limit_counts_containers_from_the_envelope_root(self):
        make_envelope(payload=nested_object(STAGE_ENVELOPE_MAX_DEPTH - 2))
        make_envelope(
            payload={"list": [[["scalar at depth limit does not add depth"]]]}
        )
        with self.assertRaises(StageEnvelopeError) as raised:
            make_envelope(
                metadata={"plugin.deep": nested_object(STAGE_ENVELOPE_MAX_DEPTH - 2)}
            )
        self.assertEqual(StageEnvelopeError.LIMIT_EXCEEDED, raised.exception.code)

    def test_payload_and_metadata_are_frozen_and_to_dict_returns_independent_copies(
        self,
    ):
        source_payload = {"items": [{"id": "a"}]}
        envelope = make_envelope(
            payload=source_payload, metadata={"plugin.note": {"k": [1]}}
        )
        source_payload["items"].append({"id": "mutated"})

        with self.assertRaises(TypeError):
            envelope.payload["items"] = []  # type: ignore[index]
        exported = envelope.to_dict()
        exported["payload"]["items"].append({"id": "exported"})
        exported["metadata"]["plugin.note"]["k"].append(2)

        self.assertEqual([{"id": "a"}], envelope.to_dict()["payload"]["items"])
        self.assertEqual({"plugin.note": {"k": [1]}}, envelope.to_dict()["metadata"])

    def test_replace_revalidates_modified_envelopes(self):
        envelope = make_envelope()
        modified = dataclasses.replace(
            envelope,
            producer_kind="hook",
            parent_envelope_id="env-1",
            envelope_id="env-2",
        )

        self.assertEqual("env-1", modified.parent_envelope_id)
        with self.assertRaises(StageEnvelopeError):
            dataclasses.replace(envelope, stage="unknown")

    def test_trace_and_provenance_survive_round_trip(self):
        envelope = make_envelope(
            correlation_id="corr-1",
            parent_envelope_id="env-0",
            derived_from=["env-0", "env-x"],
            frame_id="frame-1",
            turn_id="turn-1",
            monotonic_ns=5,
            confidence=0.25,
            artifact_refs=[ArtifactReference("frame.raw", "a" * 64, "image/png", True)],
        )
        restored = decode_stage_envelope(encode_stage_envelope(envelope))

        self.assertEqual(envelope, restored)
        self.assertEqual(("env-0", "env-x"), restored.derived_from)
        self.assertEqual(
            ("trace-1", "corr-1", "env-0"),
            (restored.trace_id, restored.correlation_id, restored.parent_envelope_id),
        )

    def test_from_dict_rejects_non_object_and_wrong_container_types(self):
        document = example_document()
        for field_name, value in (
            ("derived_from", "env-1"),
            ("artifact_refs", {}),
            ("artifact_refs", ["x"]),
        ):
            with self.subTest(field=field_name, value=value):
                broken = copy.deepcopy(document)
                broken[field_name] = value
                with self.assertRaises(StageEnvelopeError) as raised:
                    StageEnvelope.from_dict(broken)
                self.assertEqual(StageEnvelopeError.MALFORMED, raised.exception.code)
        with self.assertRaises(StageEnvelopeError):
            StageEnvelope.from_dict(["not", "an", "object"])  # type: ignore[arg-type]


class StageEnvelopeCodecTest(unittest.TestCase):
    def test_encoding_is_canonical_and_independent_of_key_order(self):
        document = example_document("hook_decision_input.json")
        reordered = dict(reversed(list(document.items())))

        first = encode_stage_envelope(StageEnvelope.from_dict(document))
        second = encode_stage_envelope(StageEnvelope.from_dict(reordered))

        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(
                json.loads(first),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode(),
            first,
        )
        self.assertIn("スタート".encode(), first)

    def test_encode_and_decode_reject_documents_over_the_byte_limit(self):
        oversized = make_envelope(
            payload={"blob": "x" * STAGE_ENVELOPE_MAX_ENCODED_BYTES}
        )
        with self.assertRaises(StageEnvelopeError) as encoded:
            encode_stage_envelope(oversized)
        with self.assertRaises(StageEnvelopeError) as decoded:
            decode_stage_envelope(b" " * (STAGE_ENVELOPE_MAX_ENCODED_BYTES + 1))

        self.assertEqual(StageEnvelopeError.LIMIT_EXCEEDED, encoded.exception.code)
        self.assertEqual(StageEnvelopeError.LIMIT_EXCEEDED, decoded.exception.code)

    def test_deep_nesting_is_rejected_before_recursive_parsing(self):
        with self.assertRaises(StageEnvelopeError) as raised:
            decode_stage_envelope("[" * 200_000 + "]" * 200_000)

        self.assertEqual(StageEnvelopeError.LIMIT_EXCEEDED, raised.exception.code)

    def test_rejects_invalid_text_encodings(self):
        valid = encode_stage_envelope(StageEnvelope.from_dict(example_document()))
        for data in (b"\xef\xbb\xbf" + valid, b"\xff" + valid, "\ud800", 123):
            with self.subTest(data=repr(data)[:20]):
                with self.assertRaises(StageEnvelopeError) as raised:
                    decode_stage_envelope(data)  # type: ignore[arg-type]
                self.assertEqual(StageEnvelopeError.MALFORMED, raised.exception.code)

    def test_unmatched_closing_bracket_cannot_lower_the_depth_count(self):
        text = "]" * 10 + "[" * 40

        with self.assertRaises(StageEnvelopeError) as raised:
            decode_stage_envelope(text)

        self.assertEqual(StageEnvelopeError.MALFORMED, raised.exception.code)

    def test_huge_integer_confidence_is_malformed_without_overflow(self):
        with self.assertRaises(StageEnvelopeError) as raised:
            make_envelope(confidence=10**400)

        self.assertEqual(StageEnvelopeError.MALFORMED, raised.exception.code)

    def test_overlong_integer_literal_is_malformed(self):
        text = json.dumps(example_document()).replace(
            '"payload": {}', '"payload": {"n": ' + "9" * 5000 + "}"
        )

        with self.assertRaises(StageEnvelopeError) as raised:
            decode_stage_envelope(text)

        self.assertEqual(StageEnvelopeError.MALFORMED, raised.exception.code)


class StageEnvelopeMigrationTest(unittest.TestCase):
    def test_registered_migration_chain_upgrades_old_documents(self):
        legacy = example_document()
        legacy["schema_version"] = 0
        legacy["trace"] = legacy.pop("trace_id")
        original = copy.deepcopy(legacy)

        def migrate_v0(document):
            upgraded = dict(document)
            upgraded["trace_id"] = upgraded.pop("trace")
            upgraded["schema_version"] = 1
            return upgraded

        envelope = decode_stage_envelope(json.dumps(legacy), migrations={0: migrate_v0})

        self.assertEqual("trace-1", envelope.trace_id)
        self.assertEqual(original, legacy)

    def test_missing_or_non_advancing_migrations_are_unsupported(self):
        legacy = example_document()
        legacy["schema_version"] = 0
        for migrations in (
            {},
            {0: lambda document: dict(document)},
            {0: lambda document: {**document, "schema_version": 3}},
        ):
            with self.subTest(migrations=migrations):
                with self.assertRaises(StageEnvelopeError) as raised:
                    migrate_stage_envelope_document(legacy, migrations)
                self.assertEqual(
                    StageEnvelopeError.UNSUPPORTED_VERSION, raised.exception.code
                )

    def test_newer_versions_are_never_migrated(self):
        newer = example_document()
        newer["schema_version"] = 2

        with self.assertRaises(StageEnvelopeError) as raised:
            migrate_stage_envelope_document(newer, {2: lambda document: document})

        self.assertEqual(StageEnvelopeError.UNSUPPORTED_VERSION, raised.exception.code)


class StageEnvelopeDependencyTest(unittest.TestCase):
    def test_contract_modules_do_not_import_core_pipeline_objects(self):
        allowed = {"ai_game_player.experience", "ai_game_player.stage_envelope"}
        package_dir = REPOSITORY_ROOT / "src" / "ai_game_player"
        for module_name in ("stage_envelope.py", "stage_envelope_codec.py"):
            tree = ast.parse((package_dir / module_name).read_text(encoding="utf-8"))
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("ai_game_player")
            }
            with self.subTest(module=module_name):
                self.assertLessEqual(imported, allowed)


if __name__ == "__main__":
    unittest.main()
