import json
import unittest
from unittest.mock import patch

from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.decision_verifier import DecisionVerifier, ReliabilityStatus
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.provider import OllamaProvider


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class ProviderDecisionTest(unittest.TestCase):
    def setUp(self):
        self.observation = ScreenObservation("menu", 100, 80, features={"signature": "menu-v1"})
        self.candidate = ActionCandidate("start", "wait", "Start", confidence=0.9)
        self.context = DecisionContextBuilder().build(
            self.observation,
            [self.candidate],
            [self.candidate],
        )

    @patch("ai_game_player.provider.urlopen")
    def test_preserves_snapshot_references_for_the_verifier(self, urlopen):
        urlopen.return_value = FakeResponse(
            {
                "response": json.dumps(
                    {
                        "action_id": "start",
                        "reason": "Start is the allowed action",
                        "snapshot_id": self.context.snapshot_id,
                        "screen_id": "menu",
                        "state_signature": "menu-v1",
                    }
                )
            }
        )

        decision = OllamaProvider("small-model").choose_context(self.context)
        result = DecisionVerifier().verify(decision, self.context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.TRUST)
        self.assertEqual(decision.snapshot_id, self.context.snapshot_id)

    @patch("ai_game_player.provider.urlopen")
    def test_does_not_coerce_invalid_action_id_type(self, urlopen):
        urlopen.return_value = FakeResponse(
            {"response": json.dumps({"action_id": 7, "reason": "start"})}
        )

        decision = OllamaProvider("small-model").choose_context(self.context)
        result = DecisionVerifier().verify(decision, self.context, self.observation)

        self.assertEqual(decision.action_id, 7)
        self.assertEqual(result.status, ReliabilityStatus.REJECT)

    @patch("ai_game_player.provider.urlopen")
    def test_converts_malformed_json_to_rejectable_schema_evidence(self, urlopen):
        urlopen.return_value = FakeResponse({"response": "not-json"})

        decision = OllamaProvider("small-model").choose_context(self.context)
        result = DecisionVerifier().verify(decision, self.context, self.observation)

        self.assertIsNotNone(decision.validation_error)
        self.assertEqual(result.status, ReliabilityStatus.REJECT)
        self.assertEqual(result.provider, "ollama:small-model")

    @patch("ai_game_player.provider.urlopen")
    def test_rejects_empty_allowed_actions_before_network_request(self, urlopen):
        context = DecisionContextBuilder().build(
            self.observation,
            [self.candidate],
            [],
        )

        with self.assertRaisesRegex(ValueError, "許可された操作候補がありません"):
            OllamaProvider("small-model").choose_context(context)

        urlopen.assert_not_called()

    @patch("ai_game_player.provider.urlopen")
    def test_legacy_choose_rejects_action_outside_allowed_candidates(self, urlopen):
        urlopen.return_value = FakeResponse(
            {"response": json.dumps({"action_id": "not-allowed", "reason": "choose another action"})}
        )

        with self.assertRaisesRegex(ValueError, "許可候補外"):
            OllamaProvider("small-model").choose([self.candidate], self.observation)

    @patch("ai_game_player.provider.urlopen")
    def test_legacy_choose_keeps_empty_reason_for_missing_field(self, urlopen):
        urlopen.return_value = FakeResponse({"response": json.dumps({"action_id": "start"})})

        decision = OllamaProvider("small-model").choose([self.candidate], self.observation)

        self.assertEqual(decision.reason, "")

    @patch("ai_game_player.provider.urlopen")
    def test_legacy_choose_rejects_non_string_reason(self, urlopen):
        for reason in (None, 7):
            with self.subTest(reason=reason):
                urlopen.return_value = FakeResponse(
                    {"response": json.dumps({"action_id": "start", "reason": reason})}
                )

                with self.assertRaisesRegex(ValueError, "判断理由が文字列ではありません"):
                    OllamaProvider("small-model").choose([self.candidate], self.observation)


if __name__ == "__main__":
    unittest.main()
