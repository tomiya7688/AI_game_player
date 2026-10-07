import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.decision_verifier import DecisionVerifier, ReliabilityStatus
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation


class DecisionVerifierTest(unittest.TestCase):
    def setUp(self):
        self.verifier = DecisionVerifier()
        self.observation = ScreenObservation("menu", 100, 80, features={"signature": "menu-v1"})
        self.candidate = ActionCandidate("start", "wait", "Start", confidence=0.9)
        self.context = DecisionContextBuilder().build(
            self.observation,
            [self.candidate],
            [self.candidate],
        )

    def decision(self, **overrides):
        fields = {
            "action_id": "start",
            "reason": "Start is the highest-rated allowed action",
            "provider": "test-provider",
            "snapshot_id": self.context.snapshot_id,
            "screen_id": "menu",
            "state_signature": "menu-v1",
        }
        fields.update(overrides)
        return ActionDecision(**fields)

    def test_trusts_action_grounded_in_current_snapshot_and_scene(self):
        result = self.verifier.verify(self.decision(), self.context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.TRUST)
        self.assertEqual(result.action_id, "start")
        self.assertEqual(result.snapshot_id, self.context.snapshot_id)
        self.assertTrue(all(item.source == "decision_verifier/v1" for item in result.evidence))
        self.assertEqual(result.to_dict()["schema"], "reliability/v1")

    def test_rejects_invalid_schema_types_and_unexpected_fields(self):
        for decision in (
            {"action_id": 7, "reason": "start", "provider": "test-provider"},
            {"action_id": "start", "reason": "start", "provider": "test-provider", "coordinate": [1, 2]},
            object(),
        ):
            with self.subTest(decision=decision):
                result = self.verifier.verify(decision, self.context, self.observation)
                self.assertEqual(result.status, ReliabilityStatus.REJECT)
                self.assertEqual(result.evidence[0].check, "decision_schema")

    def test_rejects_action_outside_allowed_candidate_set(self):
        result = self.verifier.verify(self.decision(action_id="quit"), self.context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.REJECT)
        self.assertIn("allowed_candidate_grounding", {item.check for item in result.evidence})

    def test_rejects_stale_snapshot_and_state_references(self):
        stale_snapshot = self.verifier.verify(
            self.decision(snapshot_id="previous-snapshot"), self.context, self.observation
        )
        stale_state = self.verifier.verify(
            self.decision(state_signature="previous-state"), self.context, self.observation
        )

        self.assertEqual(stale_snapshot.status, ReliabilityStatus.REJECT)
        self.assertEqual(stale_state.status, ReliabilityStatus.REJECT)
        self.assertTrue(any(item.check == "snapshot_reference" for item in stale_snapshot.evidence))
        self.assertTrue(any(item.check == "state_reference" for item in stale_state.evidence))

    def test_rejects_context_from_a_different_screen(self):
        different_screen = ScreenObservation("battle", 100, 80, features={"signature": "menu-v1"})

        result = self.verifier.verify(self.decision(), self.context, different_screen)

        self.assertEqual(result.status, ReliabilityStatus.REJECT)
        self.assertTrue(any(item.check == "context_scene_consistency" for item in result.evidence))

    def test_rejects_reason_that_explicitly_denies_selected_action(self):
        for reason in (
            "Do not select Start because it is not appropriate",
            "Startは選択しない",
        ):
            with self.subTest(reason=reason):
                result = self.verifier.verify(self.decision(reason=reason), self.context, self.observation)

                self.assertEqual(result.status, ReliabilityStatus.REJECT)
                self.assertTrue(any(item.check == "reason_action_consistency" for item in result.evidence))

    def test_normalizes_candidate_separators_when_checking_negation(self):
        candidate = ActionCandidate("quit_game", "wait", "Exit", confidence=0.9)
        context = DecisionContextBuilder().build(self.observation, [candidate], [candidate])
        decision = ActionDecision(
            "quit_game",
            "Do not select quit-game",
            "test-provider",
            context.snapshot_id,
            "menu",
            "menu-v1",
        )

        result = self.verifier.verify(decision, context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.REJECT)
        self.assertTrue(any(item.check == "reason_action_consistency" for item in result.evidence))

    def test_does_not_treat_negated_description_as_selection_rejection(self):
        candidate = ActionCandidate("exit", "wait", "Exit", confidence=0.9)
        context = DecisionContextBuilder().build(self.observation, [candidate], [candidate])
        decision = ActionDecision(
            "exit",
            "Exit is not dangerous, select Exit",
            "test-provider",
            context.snapshot_id,
            "menu",
            "menu-v1",
        )

        result = self.verifier.verify(decision, context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.TRUST)

    def test_does_not_match_english_prefixes_of_different_candidate_phrases(self):
        longer_candidate = ActionCandidate("start-over", "wait", "Start Over", confidence=0.9)
        candidate_sets = ([self.candidate, longer_candidate],)
        for candidates in candidate_sets:
            context = DecisionContextBuilder().build(self.observation, candidates, candidates)
            for reason in (
                "Avoid startup delay; select Start",
                "Do not select Start Over; select Start",
                "Do not click on Start Over; select Start",
            ):
                with self.subTest(reason=reason, candidate_count=len(candidates)):
                    decision = ActionDecision(
                        "start",
                        reason,
                        "test-provider",
                        context.snapshot_id,
                        "menu",
                        "menu-v1",
                    )
                    result = self.verifier.verify(decision, context, self.observation)

                    self.assertEqual(result.status, ReliabilityStatus.TRUST)

    def test_rejects_english_negation_with_ordinary_trailing_modifiers(self):
        for reason in (
            "Do not select Start now",
            "Do not select Start please",
            "Avoid Start at all costs",
            "Do not press the Start button yet",
            "Do not click on Start under any circumstances",
            "Do not select Start Now; select Start",
        ):
            with self.subTest(reason=reason):
                result = self.verifier.verify(self.decision(reason=reason), self.context, self.observation)

                self.assertEqual(result.status, ReliabilityStatus.REJECT)

    def test_rejects_english_negation_of_click_and_press_actions(self):
        candidate = ActionCandidate("start", "click", "Start", x=20, y=20, confidence=0.9)
        context = DecisionContextBuilder().build(self.observation, [candidate], [candidate])
        for reason in (
            "Do not click Start",
            "Do not click on Start",
            "Do not press Start",
            "Do not press the Start button",
            "Do not double-click Start",
            "Do not double-click the Start button",
            "Start should not be clicked",
            "Start must not be pressed",
            "Start should not be double-clicked",
            "The Start button should not be clicked",
        ):
            with self.subTest(reason=reason):
                decision = ActionDecision(
                    "start",
                    reason,
                    "test-provider",
                    context.snapshot_id,
                    "menu",
                    "menu-v1",
                )
                result = self.verifier.verify(
                    decision,
                    context,
                    self.observation,
                )

                self.assertEqual(result.status, ReliabilityStatus.REJECT)
                self.assertTrue(any(item.check == "reason_action_consistency" for item in result.evidence))

    def test_does_not_reject_japanese_negation_of_negation(self):
        for reason in (
            "Startは不適切ではないため選択する",
            "Startを避ける必要はない",
            "Startを避ける必要がありません",
            "Startを避けるわけではない",
        ):
            with self.subTest(reason=reason):
                result = self.verifier.verify(self.decision(reason=reason), self.context, self.observation)

                self.assertEqual(result.status, ReliabilityStatus.TRUST)

    # {
    #   責務: [長い別候補への否定を選択候補への否定として誤判定しないことを確認する]
    #   処理: [1: 「終了」と「終了確認」を候補にする 2: 別候補を避けて選択候補を選ぶ理由は信頼する 3: 選択候補自体を避ける理由は拒否する]
    #   引数: []
    #   戻り値: [None: 信頼性判定の結果を検証する]
    #   エラー: []
    # }
    def test_japanese_negation_of_longer_candidate_does_not_reject_selected_candidate(self):
        selected = ActionCandidate("finish", "wait", "終了", confidence=0.9)
        confirmation = ActionCandidate("finish-confirmation", "wait", "終了確認", confidence=0.9)
        candidates = [selected, confirmation]
        context = DecisionContextBuilder().build(self.observation, candidates, candidates)

        for reason in (
            "終了確認を避けるため終了を選択する",
            "終了確認は避けるので終了を選択する",
        ):
            with self.subTest(reason=reason):
                decision = ActionDecision(
                    "finish",
                    reason,
                    "test-provider",
                    context.snapshot_id,
                    "menu",
                    "menu-v1",
                )
                result = self.verifier.verify(decision, context, self.observation)

                self.assertEqual(result.status, ReliabilityStatus.TRUST)

        rejected = self.verifier.verify(
            ActionDecision(
                "finish",
                "終了確認を避けるため終了を避ける",
                "test-provider",
                context.snapshot_id,
                "menu",
                "menu-v1",
            ),
            context,
            self.observation,
        )
        self.assertEqual(rejected.status, ReliabilityStatus.REJECT)

    def test_rejects_negation_of_one_character_action(self):
        candidate = ActionCandidate("A", "wait", "A", confidence=0.9)
        context = DecisionContextBuilder().build(self.observation, [candidate], [candidate])
        decision = ActionDecision(
            "A",
            "Do not select A",
            "test-provider",
            context.snapshot_id,
            "menu",
            "menu-v1",
        )

        result = self.verifier.verify(decision, context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.REJECT)
        self.assertTrue(any(item.check == "reason_action_consistency" for item in result.evidence))

    def test_missing_snapshot_reference_requires_verification(self):
        result = self.verifier.verify(self.decision(snapshot_id=None), self.context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.VERIFY)
        self.assertTrue(any(item.severity == "verify" for item in result.evidence))

    def test_empty_reason_is_caution_without_claiming_trust(self):
        result = self.verifier.verify(self.decision(reason=""), self.context, self.observation)

        self.assertEqual(result.status, ReliabilityStatus.CAUTION)

    def test_rejection_is_persisted_before_engine_fails_closed(self):
        class InvalidProvider:
            def choose_context(self, context, personality=""):
                return ActionDecision(
                    "outside",
                    "Choose outside the allowed set",
                    "invalid-provider",
                    context.snapshot_id,
                    context.state["screen_id"],
                    context.state["signature"],
                )

        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory), provider=InvalidProvider())
            with self.assertRaisesRegex(ValueError, "reliability verifier"):
                engine.step(self.observation, [self.candidate])

            trace = json.loads((Path(directory) / "decision_trace.json").read_text(encoding="utf-8"))

        self.assertEqual(len(trace), 1)
        self.assertIsNone(trace[0]["decision"])
        self.assertEqual(trace[0]["reliability"]["status"], "REJECT")

    def test_engine_preserves_evaluator_grounding_when_context_builder_is_overpermissive(self):
        class OverpermissiveContextBuilder(DecisionContextBuilder):
            def build(self, observation, candidates, allowed_candidates, **kwargs):
                return super().build(observation, candidates, candidates, **kwargs)

        class Provider:
            def choose_context(self, context, personality=""):
                return ActionDecision(
                    "weak",
                    "Choose the only candidate",
                    "test-provider",
                    context.snapshot_id,
                    context.state["screen_id"],
                    context.state["signature"],
                )

        weak_candidate = ActionCandidate("weak", "wait", "Weak", confidence=0.2)
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(
                Path(directory),
                provider=Provider(),
                context_builder=OverpermissiveContextBuilder(),
            )

            with self.assertRaisesRegex(ValueError, "allowed_candidate_grounding"):
                engine.step(self.observation, [weak_candidate])

        self.assertEqual(engine.last_reliability_result.status, ReliabilityStatus.REJECT)
        grounding = [
            item for item in engine.last_reliability_result.evidence if item.check == "allowed_candidate_grounding"
        ]
        self.assertTrue(any(item.severity == "reject" for item in grounding))


if __name__ == "__main__":
    unittest.main()
