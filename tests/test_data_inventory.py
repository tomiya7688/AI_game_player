import ast
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.data_class_policy import DataClassPolicy, DataClassPolicyError
from ai_game_player.data_inventory import (
    DEFAULT_DATA_INVENTORY_PATH,
    DataInventory,
    DataInventoryError,
    load_data_inventory,
)
from ai_game_player.retention_policy import RetentionPolicy

REPOSITORY_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "ai_game_player"
SCHEMA_PATH = REPOSITORY_ROOT / "config" / "data_inventory.schema.json"
PROVIDER_SCHEMAS = REPOSITORY_ROOT / "schemas" / "inference_provider" / "v1"
HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None

# 永続化とみなす呼出し名。open系はmodeで判定する。
WRITE_CALLS = frozenset(
    {
        "write_text",
        "write_bytes",
        "stage_json_write",
        "write_json_array_atomically",
        "StagedJsonWrite",
        "NamedTemporaryFile",
        "mkstemp",
    }
)
# 端末外へ送り得る呼出し名（宛先endpointが利用者設定でremoteになり得るもの）。
SEND_CALLS = frozenset(
    {"urlopen", "create_connection", "HTTPConnection", "HTTPSConnection"}
)
SEND_MODULES = frozenset({"requests", "httpx", "socket", "aiohttp"})
PATH_SUFFIXES = (".json", ".jsonl", ".sqlite3", ".db", ".log", ".png", ".mp4")


def document() -> dict:
    return json.loads(DEFAULT_DATA_INVENTORY_PATH.read_text(encoding="utf-8"))


def module_name(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT).with_suffix("")
    return ".".join(("ai_game_player", *relative.parts))


def call_name(call: ast.Call) -> tuple[str | None, str | None]:
    function = call.func
    if isinstance(function, ast.Attribute):
        base = function.value.id if isinstance(function.value, ast.Name) else None
        return function.attr, base
    if isinstance(function, ast.Name):
        return function.id, None
    return None, None


def open_mode(call: ast.Call) -> object:
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    # 組込みopen(path, mode)とPath.open(mode)で位置引数の位置が違う。
    index = 1 if isinstance(call.func, ast.Name) else 0
    if len(call.args) > index and isinstance(call.args[index], ast.Constant):
        return call.args[index].value
    return None


# remote:neverなのに現行の送信payloadへ入るclass（送信箇所→class）。inventoryは事実どおり宣言し、
# 不変条件の例外はここに明示したものだけに限る。#226（Remote Consent Enforcement）で解消して空にする。
KNOWN_REMOTE_GAP: dict[str, frozenset[str]] = {
    "ai_game_player.provider:OllamaProvider._request_decision": frozenset(
        {"knowledge"}
    ),
}


class SourceScan:
    """Collect persistence/transmission sites and storage literals from src."""

    def __init__(self) -> None:
        self.writers: set[str] = set()
        self.transmitters: set[str] = set()
        self.path_literals: set[str] = set()
        self.artifact_types: set[str] = set()
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            self._visit(module_name(path), tree, [])

    def _visit(self, module: str, node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._defaults(child)
                self._visit(module, child, [*scope, child.name])
                continue
            if isinstance(child, ast.ClassDef):
                self._visit(module, child, [*scope, child.name])
                continue
            if isinstance(child, ast.JoinedStr):
                # f-stringの固定部分（`.json`等）はpath名ではない。保存先directory名で分類する。
                continue
            if isinstance(child, ast.Call):
                self._call(module, child, scope)
            self._literal(child)
            self._visit(module, child, scope)

    def _call(self, module: str, call: ast.Call, scope: list[str]) -> None:
        name, base = call_name(call)
        top_level = scope[0] if scope else "<module>"
        persisted = name in WRITE_CALLS or (name == "connect" and base == "sqlite3")
        if name == "open":
            mode = open_mode(call)
            persisted = isinstance(mode, str) and any(flag in mode for flag in "wax+")
        if persisted:
            self.writers.add(f"{module}:{top_level}")
        if name in SEND_CALLS or base in SEND_MODULES:
            self.transmitters.add(f"{module}:{'.'.join(scope) or '<module>'}")
        if name == "Path" and call.args and isinstance(call.args[0], ast.Constant):
            if isinstance(call.args[0].value, str):
                self.path_literals.add(call.args[0].value)
        for keyword in call.keywords:
            if keyword.arg in {"artifact_type", "expected_artifact_type"}:
                if isinstance(keyword.value, ast.Constant) and isinstance(
                    keyword.value.value, str
                ):
                    self.artifact_types.add(keyword.value.value)

    def _defaults(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments = function.args
        positional = [*arguments.posonlyargs, *arguments.args]
        pairs = list(zip(positional[-len(arguments.defaults) :], arguments.defaults))
        pairs += [
            (argument, default)
            for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults)
            if default is not None
        ]
        for argument, default in pairs:
            if (
                argument.arg == "artifact_type"
                and isinstance(default, ast.Constant)
                and isinstance(default.value, str)
            ):
                self.artifact_types.add(default.value)

    def _literal(self, node: ast.AST) -> None:
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
        ):
            self.path_literals.add(node.right.value)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith(PATH_SUFFIXES)
            and "\n" not in node.value
            and " " not in node.value
        ):
            self.path_literals.add(node.value)


SCAN = SourceScan()


class DataInventoryRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = load_data_inventory()

    @unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is a development dependency")
    def test_canonical_document_matches_schema(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document())

    def test_issue_scope_classes_and_credential_class_are_defined(self) -> None:
        required = {
            "raw_frame",
            "cropped_image",
            "ocr_text",
            "ui_metadata",
            "decision_context",
            "model_output",
            "trace",
            "video",
            "replay",
            "learning_dataset",
            "challenger_artifact",
            "crash_diagnostic",
            "credential",
        }
        self.assertLessEqual(required, set(self.inventory.classes))
        credential = self.inventory.policy("credential")
        self.assertEqual("forbidden", credential.sensitivity)
        self.assertEqual("never", credential.default_persistence)
        self.assertFalse(credential.may_leave_machine)
        self.assertIn("api_key", self.inventory.forbidden_field_names)

    def test_1_0_defaults_are_local_first(self) -> None:
        self.assertEqual("1.0", self.inventory.policy_version)
        for data_class in ("raw_frame", "cropped_image", "replay", "video"):
            policy = self.inventory.policy(data_class)
            self.assertEqual("off", policy.default_persistence, data_class)
            self.assertFalse(policy.persisted_by_default, data_class)
        trace = self.inventory.policy("trace")
        self.assertEqual("local_bounded", trace.default_persistence)
        self.assertIsNotNone(trace.default_retention_ms)
        for policy in self.inventory.classes.values():
            # remoteは同意後に送れるかだけを表し、既定で送信する値は存在しない。
            self.assertIn(policy.remote, {"never", "opt_in"})
            if policy.sensitivity == "high":
                self.assertNotEqual("local", policy.default_persistence)

    def test_1_0_default_table_is_pinned(self) -> None:
        # 既定の変更は意図的であるべきなので、全classの保存modeと送信可否を固定する。
        expected = {
            "raw_frame": ("off", "opt_in"),
            "cropped_image": ("off", "opt_in"),
            "ocr_text": ("off", "opt_in"),
            "ui_metadata": ("local_bounded", "opt_in"),
            "decision_context": ("off", "opt_in"),
            "model_output": ("local_bounded", "opt_in"),
            "history": ("local_bounded", "opt_in"),
            "trace": ("local_bounded", "opt_in"),
            "video": ("off", "never"),
            "replay": ("off", "never"),
            "learning_dataset": ("off", "never"),
            "challenger_artifact": ("local_bounded", "never"),
            "knowledge": ("local", "never"),
            "crash_diagnostic": ("local_bounded", "never"),
            "safety_audit": ("local_bounded", "never"),
            "runtime_log": ("local_bounded", "never"),
            "app_config": ("local", "never"),
            "quality_report": ("local", "never"),
            "worker_ipc": ("transient", "never"),
            "unclassified": ("off", "never"),
            "credential": ("never", "never"),
        }
        actual = {
            name: (policy.default_persistence, policy.remote)
            for name, policy in self.inventory.classes.items()
        }
        self.assertEqual(expected, actual)

    def test_policy_queries(self) -> None:
        self.assertEqual(
            "knowledge",
            self.inventory.class_for_artifact_type("knowledge.snapshot").data_class,
        )
        self.assertEqual(
            {"trace", "decision_context", "model_output", "ocr_text"}
            | {"ui_metadata", "history"},
            set(self.inventory.effective_classes(["trace"])),
        )
        self.assertIn("raw_frame", self.inventory.remote_eligible_classes())
        self.assertNotIn("credential", self.inventory.remote_eligible_classes())
        with self.assertRaises(DataInventoryError):
            self.inventory.policy("telemetry")
        with self.assertRaises(DataInventoryError):
            self.inventory.class_for_artifact_type("unregistered.type")

    def test_forbidden_field_paths_find_nested_credentials(self) -> None:
        payload = {
            "decision_context": {"state": {"ocr": ["hello"]}},
            "headers": [{"Authorization": "x"}, {"X-Api-Key": "y"}],
            "openai_api_key": "z",
            "max_tokens": 3,
        }
        self.assertEqual(
            (
                "$.headers[0].Authorization",
                "$.headers[1].X-Api-Key",
                "$.openai_api_key",
            ),
            self.inventory.forbidden_field_paths(payload),
        )
        self.assertEqual((), self.inventory.forbidden_field_paths({"tokens": 1}))
        cyclic: dict = {"password": 1}
        cyclic["self"] = cyclic
        self.assertEqual(("$.password",), self.inventory.forbidden_field_paths(cyclic))

    def test_retention_ttl_defaults_feed_retention_policy(self) -> None:
        ttl_by_type = self.inventory.ttl_ms_by_artifact_type()
        self.assertEqual(set(self.inventory.artifact_types), set(ttl_by_type))
        policy = RetentionPolicy(
            soft_budget_bytes=10,
            hard_limit_bytes=20,
            default_ttl_ms=self.inventory.policy("unclassified").default_retention_ms,
            ttl_ms_by_type=ttl_by_type,
        )
        day_ms = 86_400_000
        self.assertEqual(1_000 + day_ms, policy.expiry_for("frame.raw", 1_000))
        self.assertIsNone(policy.expiry_for("knowledge.snapshot", 1_000))
        self.assertEqual(day_ms, policy.expiry_for("future.type", 0))

    def test_owner_modules_exist(self) -> None:
        for policy in self.inventory.classes.values():
            for module in policy.owner_modules:
                relative = Path(*module.split(".")[1:])
                exists = (SOURCE_ROOT / relative).with_suffix(".py").is_file() or (
                    SOURCE_ROOT / relative / "__init__.py"
                ).is_file()
                self.assertTrue(exists, f"{policy.data_class}: {module}")

    def test_wire_enums_equal_remote_eligible_classes(self) -> None:
        remote = self.inventory.remote_eligible_classes()
        for name, pointer in (
            ("inference_request.schema.json", ("privacy", "data_classes")),
            ("provider_manifest.schema.json", ("privacy", "declared_data_classes")),
        ):
            schema = json.loads((PROVIDER_SCHEMAS / name).read_text(encoding="utf-8"))
            node = schema["properties"][pointer[0]]["properties"][pointer[1]]
            self.assertEqual(set(node["items"]["enum"]), remote, name)


class DataInventoryCoverageTest(unittest.TestCase):
    """Fail when src persists or transmits something the inventory does not classify."""

    def setUp(self) -> None:
        self.inventory = load_data_inventory()

    def test_every_persistence_site_is_classified(self) -> None:
        self.assertEqual(
            set(),
            SCAN.writers - set(self.inventory.writers),
            "新しい永続化箇所をdata_inventory.jsonのwritersへ分類してください",
        )
        self.assertEqual(set(), set(self.inventory.writers) - SCAN.writers, "stale")

    def test_every_transmission_site_is_classified_and_remote_eligible(self) -> None:
        self.assertEqual(
            set(),
            SCAN.transmitters - set(self.inventory.transmitters),
            "新しい送信箇所をdata_inventory.jsonのtransmittersへ分類してください",
        )
        self.assertEqual(
            set(), set(self.inventory.transmitters) - SCAN.transmitters, "stale"
        )
        remote = self.inventory.remote_eligible_classes()
        for site, classes in self.inventory.transmitters.items():
            gap = KNOWN_REMOTE_GAP.get(site, frozenset())
            self.assertLessEqual(
                set(self.inventory.effective_classes(classes)) - gap, remote, site
            )
        # 既知の差分は実際に宣言されていて、remote:neverのままであること（#226で解消し空にする）
        for site, gap in KNOWN_REMOTE_GAP.items():
            self.assertLessEqual(gap, set(self.inventory.transmitters[site]), site)
            self.assertFalse(gap & remote, site)

    def test_every_storage_path_literal_is_classified(self) -> None:
        classified = set(self.inventory.persisted_paths) | set(
            self.inventory.non_data_paths
        )
        self.assertEqual(set(), SCAN.path_literals - classified)
        self.assertEqual(set(), classified - SCAN.path_literals, "stale")

    def test_every_artifact_type_in_source_is_mapped(self) -> None:
        self.assertTrue(SCAN.artifact_types)
        self.assertEqual(
            set(), SCAN.artifact_types - set(self.inventory.artifact_types)
        )


class DataInventoryValidationTest(unittest.TestCase):
    def load(self, value: dict) -> DataInventory:
        return DataInventory.from_dict(value)

    def find_class(self, value: dict, data_class: str) -> dict:
        return next(item for item in value["classes"] if item["id"] == data_class)

    def assert_rejected(self, value: dict) -> None:
        with self.assertRaises(DataInventoryError):
            self.load(value)

    def test_rejects_version_and_unknown_fields(self) -> None:
        value = document()
        value["schema_version"] = 2
        self.assert_rejected(value)
        value = document()
        value["telemetry"] = True
        self.assert_rejected(value)
        self.assert_rejected([])

    def test_rejects_unknown_class_references(self) -> None:
        for mutate in (
            lambda v: v["artifact_types"].update({"x.y": "nope"}),
            lambda v: v["writers"][0].update({"data_class": "nope"}),
            lambda v: v["transmitters"][0].update({"data_classes": ["nope"]}),
            lambda v: self.find_class(v, "trace")["contains"].append("nope"),
        ):
            value = document()
            mutate(value)
            self.assert_rejected(value)

    def test_rejects_non_privacy_preserving_defaults(self) -> None:
        cases = (
            ("credential", {"default_persistence": "local_bounded"}),
            ("credential", {"remote": "opt_in"}),
            ("raw_frame", {"default_persistence": "local"}),
            ("trace", {"default_retention_ms": None}),
            ("trace", {"remote": "always"}),
            ("ocr_text", {"default_retention_ms": 0}),
            ("knowledge", {"default_retention_ms": 1}),
            ("worker_ipc", {"default_retention_ms": 1}),
        )
        for data_class, change in cases:
            value = document()
            self.find_class(value, data_class).update(change)
            with self.subTest(data_class=data_class, change=change):
                self.assert_rejected(value)

    def test_rejects_inconsistent_containment(self) -> None:
        value = document()
        self.find_class(value, "model_output")["remote"] = "never"
        self.assert_rejected(value)
        value = document()
        self.find_class(value, "trace")["sensitivity"] = "medium"
        self.assert_rejected(value)
        value = document()
        # history→decision_context→historyの間接循環（機微度・送信可否は整合させる）。
        self.find_class(value, "history").update(
            {"sensitivity": "high", "contains": ["decision_context"]}
        )
        with self.assertRaisesRegex(DataInventoryError, "cycle"):
            self.load(value)

    def test_rejects_duplicates_and_missing_notes(self) -> None:
        value = document()
        value["classes"].append(copy.deepcopy(value["classes"][0]))
        self.assert_rejected(value)
        value = document()
        value["writers"].append(copy.deepcopy(value["writers"][0]))
        self.assert_rejected(value)
        value = document()
        infrastructure = next(w for w in value["writers"] if w["data_class"] is None)
        del infrastructure["note"]
        self.assert_rejected(value)
        value = document()
        value["non_data_paths"]["history.json"] = "conflict"
        self.assert_rejected(value)
        value = document()
        value["forbidden_field_names"] = []
        self.assert_rejected(value)
        value = document()
        value["classes"] = [
            item for item in value["classes"] if item["sensitivity"] != "forbidden"
        ]
        self.assert_rejected(value)

    def test_policy_rejects_implemented_class_without_owner(self) -> None:
        item = copy.deepcopy(self.find_class(document(), "raw_frame"))
        item["owner_modules"] = []
        with self.assertRaises(DataClassPolicyError):
            DataClassPolicy.from_dict(item)

    def test_rejects_non_string_transmitter_classes(self) -> None:
        value = document()
        value["transmitters"][0]["data_classes"] = [["ocr_text"]]
        self.assert_rejected(value)

    def test_load_rejects_duplicate_json_keys(self) -> None:
        text = DEFAULT_DATA_INVENTORY_PATH.read_text(encoding="utf-8")
        duplicated = text.replace(
            '"frame.raw": "raw_frame",',
            '"frame.raw": "raw_frame", "frame.raw": "knowledge",',
            1,
        )
        self.assertNotEqual(text, duplicated)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            path.write_text(duplicated, encoding="utf-8")
            with self.assertRaisesRegex(DataInventoryError, "duplicate JSON key"):
                load_data_inventory(path)

    def test_load_rejects_unreadable_or_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(DataInventoryError):
                load_data_inventory(path)
            with self.assertRaises(DataInventoryError):
                load_data_inventory(Path(directory) / "missing.json")


if __name__ == "__main__":
    unittest.main()
