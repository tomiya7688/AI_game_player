import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_game_player.data_inventory import load_data_inventory
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.execution_receipt import ExecutionReceipt
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.provider import OllamaProvider

HAS_JSONSCHEMA = importlib.util.find_spec("jsonschema") is not None
PROVIDER_SCHEMAS = Path(__file__).parents[1] / "schemas" / "inference_provider" / "v1"

# 利用者dataを含まないprotocol定数・設定。data classではない。
NON_DATA = "<non-data>"
BODY_KEYS = {"model": NON_DATA, "stream": NON_DATA, "format": NON_DATA}

# path（"/"区切り、list要素は"[]"）→data class。末尾"/**"はそのpath自体と配下全て。
# 最長一致・完全一致優先。新しい送信fieldはここへ分類しない限りtestが失敗する（Issue #806）。
FIELD_RULES: dict[str, str] = {
    "instruction": NON_DATA,
    "personality": "decision_context",
    "purpose": "decision_context",
    "observation/screen_id": "ui_metadata",
    "observation/width": "ui_metadata",
    "observation/height": "ui_metadata",
    "observation/features/**": "ui_metadata",
    "observation/ocr_text/**": "ocr_text",
    "previous/screen_id": "ui_metadata",
    "previous/width": "ui_metadata",
    "previous/height": "ui_metadata",
    "previous/features/**": "ui_metadata",
    "previous/ocr_text/**": "ocr_text",
    "allowed_actions/**": "ui_metadata",
    "decision_context/schema": "decision_context",
    "decision_context/snapshot_id": "decision_context",
    "decision_context/allowed_action_ids/**": "decision_context",
    "decision_context/goal/**": "decision_context",
    "decision_context/uncertainty/**": "decision_context",
    "decision_context/state/visible_text/**": "ocr_text",
    "decision_context/state/**": "ui_metadata",
    "decision_context/candidates/[]/action_id": "ui_metadata",
    "decision_context/candidates/[]/kind": "ui_metadata",
    "decision_context/candidates/[]/label": "ui_metadata",
    "decision_context/candidates/[]/recognition_confidence": "ui_metadata",
    "decision_context/candidates/[]/allowed": "ui_metadata",
    "decision_context/candidates/[]/uncertainty/**": "ui_metadata",
    "decision_context/candidates/[]/evaluation/**": "model_output",
    "decision_context/candidates/[]/knowledge/**": "decision_context",
    "decision_context/previous_outcome/**": "model_output",
    "decision_context/recent_history/**": "history",
    "decision_context/knowledge_projection/**": "decision_context",
    "decision_context/evaluation_projection/**": "model_output",
}
# 明示のexpose_to_models時だけ現れる項目。既定payloadでは出現しないのが正しい。
OPTIONAL_RULES = frozenset(
    {
        "decision_context/knowledge_projection/**",
        "decision_context/evaluation_projection/**",
    }
)


def _leaf_paths(value, prefix=()):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaf_paths(child, prefix + (str(key),))
    elif isinstance(value, list) and value:
        for child in value:
            yield from _leaf_paths(child, prefix + ("[]",))
    else:
        yield prefix


def _rule_matches(rule: str, path: tuple[str, ...]) -> bool:
    segments = tuple(rule.split("/"))
    if segments[-1] == "**":
        head = segments[:-1]
        return path[: len(head)] == head
    return segments == path


def classify(prompt: dict) -> tuple[dict[str, str], list[str], set[str]]:
    """leaf path→class、未分類path、一致した規則を返す。"""
    classes: dict[str, str] = {}
    unclassified: list[str] = []
    used: set[str] = set()
    for path in _leaf_paths(prompt):
        matches = [rule for rule in FIELD_RULES if _rule_matches(rule, path)]
        if not matches:
            unclassified.append("/".join(path))
            continue
        # 完全一致を優先し、次に具体的な（長い）規則
        best = max(matches, key=lambda rule: (not rule.endswith("**"), len(rule)))
        used.add(best)
        classes["/".join(path)] = FIELD_RULES[best]
    return classes, unclassified, used


def data_classes(classes: dict[str, str]) -> set[str]:
    return {value for value in classes.values() if value != NON_DATA}


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class ProviderWireDataClassesTest(unittest.TestCase):
    """OllamaProviderの実送信payloadの全fieldがdata classへ対応付けられていること。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = load_data_inventory()
        cls.captures: dict[str, list[dict]] = {}
        cls.requests: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as directory:
            cls._capture_all(Path(directory))

    @classmethod
    def _capture_all(cls, root: Path) -> None:
        observation = ScreenObservation(
            "menu",
            320,
            200,
            ["START", "OPTIONS"],
            {"signature": "state-a", "perceptual_hash": "abcd", "nested": {"k": [1]}},
        )
        previous = ScreenObservation("menu", 320, 200, ["OLD"], {"signature": "s0"})
        candidate = ActionCandidate(
            "start", "click", "START", 10, 20, bbox=(1, 2, 3, 4)
        )
        store = KnowledgeStore(root / "knowledge.json")
        store.add("strategy", "START", "safe opening action", 0.9)
        history_entry = {
            "snapshot_id": "s0",
            "screen_id": "menu",
            "state_signature": "s0",
            "action_id": "start",
            "decision": {"action_id": "start"},
            "reliability": {"status": "TRUST"},
            "action_generation": 0,
            "execution_receipt": ExecutionReceipt(
                "s0", "start", 0, "menu", "s0", "executed", True
            ).to_dict(),
        }
        context = DecisionContextBuilder(knowledge_store=store).build(
            observation,
            [candidate],
            [candidate],
            recent_history=[history_entry],
            previous_outcome=OutcomeAssessment("success", 0.8, "moved"),
            current_goal="clear game",
            short_term_goal="start game",
        )
        decision = {
            "response": json.dumps(
                {
                    "action_id": "start",
                    "reason": "ok",
                    "snapshot_id": context.snapshot_id,
                    "screen_id": "menu",
                    "state_signature": "state-a",
                }
            )
        }
        assess = {"response": json.dumps({"status": "success", "confidence": 0.9})}
        provider = OllamaProvider("small-model")
        calls = {
            "assess_outcome": (
                lambda: provider.assess_outcome(observation, previous),
                assess,
            ),
            "choose": (
                lambda: provider.choose(
                    [candidate], observation, "clear the stage", "careful"
                ),
                decision,
            ),
            "choose_context": (
                lambda: provider.choose_context(context, "careful"),
                decision,
            ),
            "list_models": (
                lambda: OllamaProvider.list_models(),
                {"models": [{"name": "m"}]},
            ),
        }
        for name, (call, response) in calls.items():
            with patch("ai_game_player.provider.urlopen") as urlopen:
                urlopen.return_value = FakeResponse(response)
                call()
            request = urlopen.call_args.args[0]
            cls.requests[name] = request
            if request.data is not None:
                body = json.loads(request.data.decode("utf-8"))
                cls.captures[name] = [body, json.loads(body["prompt"])]

    def prompts(self, *names: str) -> list[dict]:
        return [self.captures[name][1] for name in names]

    def test_body_fields_are_known_protocol_parameters(self) -> None:
        for name, (body, _) in self.captures.items():
            self.assertEqual(
                set(),
                set(body) - set(BODY_KEYS) - {"prompt"},
                f"{name}: 未分類のrequest body field",
            )

    def test_body_model_is_the_provider_setting(self) -> None:
        for name, (body, _) in self.captures.items():
            self.assertEqual("small-model", body["model"], name)
            self.assertIs(False, body["stream"], name)
            self.assertEqual("json", body["format"], name)

    def test_choose_context_payload_is_fully_classified(self) -> None:
        classes, unclassified, _ = classify(self.prompts("choose_context")[0])
        self.assertEqual([], unclassified)
        self.assertEqual(
            {"decision_context", "ocr_text", "ui_metadata", "history", "model_output"},
            data_classes(classes),
        )

    def test_legacy_choose_payload_is_fully_classified(self) -> None:
        classes, unclassified, _ = classify(self.prompts("choose")[0])
        self.assertEqual([], unclassified)
        self.assertEqual(
            {"decision_context", "ocr_text", "ui_metadata"}, data_classes(classes)
        )

    def test_assess_outcome_payload_is_fully_classified(self) -> None:
        classes, unclassified, _ = classify(self.prompts("assess_outcome")[0])
        self.assertEqual([], unclassified)
        self.assertEqual({"ocr_text", "ui_metadata"}, data_classes(classes))

    def test_list_models_sends_no_body(self) -> None:
        request = self.requests["list_models"]
        self.assertIsNone(request.data)
        self.assertEqual("GET", request.get_method())
        self.assertNotIn("list_models", self.captures)

    def test_declared_transmitter_classes_match_real_payloads(self) -> None:
        observed = {
            "ai_game_player.provider:OllamaProvider.assess_outcome": data_classes(
                classify(self.prompts("assess_outcome")[0])[0]
            ),
            "ai_game_player.provider:OllamaProvider._request_decision": data_classes(
                classify(self.prompts("choose")[0])[0]
            )
            | data_classes(classify(self.prompts("choose_context")[0])[0]),
            "ai_game_player.provider:OllamaProvider.list_models": set(),
        }
        declared = {
            site: set(classes)
            for site, classes in self.inventory.transmitters.items()
            if site.startswith("ai_game_player.provider:OllamaProvider.")
        }
        self.assertEqual(observed, declared)
        remote = self.inventory.remote_eligible_classes()
        for site, classes in observed.items():
            self.assertLessEqual(classes, remote, site)

    def test_every_rule_is_exercised_by_real_payloads(self) -> None:
        used: set[str] = set()
        for name in ("assess_outcome", "choose", "choose_context"):
            used |= classify(self.captures[name][1])[2]
        self.assertEqual(set(), set(FIELD_RULES) - used - OPTIONAL_RULES, "stale rule")

    def test_projection_fields_absent_by_default(self) -> None:
        for name in ("choose", "choose_context", "assess_outcome"):
            joined = json.dumps(self.captures[name][1])
            self.assertNotIn("knowledge_projection", joined, name)
            self.assertNotIn("evaluation_projection", joined, name)

    def test_unclassified_field_is_reported(self) -> None:
        prompt = json.loads(json.dumps(self.captures["choose_context"][1]))
        prompt["decision_context"]["screenshot_b64"] = "AAAA"
        prompt["observation_extra"] = {"x": 1}
        prompt["decision_context"]["candidates"][0]["new_field"] = 1
        _, unclassified, _ = classify(prompt)
        self.assertEqual(
            [
                "decision_context/candidates/[]/new_field",
                "decision_context/screenshot_b64",
                "observation_extra/x",
            ],
            sorted(unclassified),
        )

    def test_optional_projections_resolve_when_exposed(self) -> None:
        prompt = {
            "decision_context": {
                "knowledge_projection": {"status": "applied", "priors": {}},
                "evaluation_projection": {"candidates": {"start": {"value": 0.1}}},
            }
        }
        classes, unclassified, _ = classify(prompt)
        self.assertEqual([], unclassified)
        self.assertEqual({"decision_context", "model_output"}, data_classes(classes))

    @unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema is a development dependency")
    def test_new_classes_validate_against_schema(self) -> None:
        from jsonschema import Draft202012Validator

        request = json.loads(
            (PROVIDER_SCHEMAS / "examples" / "decision_request.json").read_text(
                encoding="utf-8"
            )
        )
        schema = json.loads(
            (PROVIDER_SCHEMAS / "inference_request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        request["privacy"]["data_classes"] = ["ui_metadata", "model_output"]
        self.assertEqual([], list(validator.iter_errors(request)))
        request["privacy"]["data_classes"] = ["knowledge"]
        self.assertTrue(list(validator.iter_errors(request)))


if __name__ == "__main__":
    unittest.main()
