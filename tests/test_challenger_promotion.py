import json
import re
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

from ai_game_player.challenger_promotion import MAX_AUDIT_EVENTS, ChampionRegistry
from ai_game_player.challenger_promotion_models import (
    CandidateEvaluation,
    PromotionCriteria,
    PromotionError,
)
from ai_game_player.challenger_update import build_challenger
from tests.test_challenger_update import (
    DECISION,
    OTHER_SCOPE,
    SCOPE,
    TIME,
    RECOGNITION,
    overconfident,
    proposals_for,
)

LATER = "2026-11-06T01:00:00Z"
CRITERIA = PromotionCriteria()


def evaluation_for(artifact, **overrides):
    values = {
        "evaluation_ref": "eval.one",
        "artifact_id": artifact.artifact_id,
        "champion_sha256": artifact.parent.sha256,
        "challenger_sha256": artifact.challenger.sha256,
        "target_cases": 20,
        "regression_cases": 20,
        "target_baseline": 0.50,
        "target_candidate": 0.60,
        "regression_baseline": 0.90,
        "regression_candidate": 0.90,
    }
    values.update(overrides)
    return CandidateEvaluation(**values)


class PromotionGateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "registry.json"
        self.registry = ChampionRegistry(self.path)
        proposal = proposals_for(overconfident(1, 2, 3)).proposals[0]
        self.artifact = build_challenger(proposal, DECISION, created_at=TIME)
        self.registry.initialize(DECISION, at=TIME)

    def decide(self, artifact=None, **overrides):
        artifact = artifact or self.artifact
        return self.registry.decide(
            artifact, evaluation_for(artifact, **overrides), CRITERIA, at=LATER
        )

    def test_improvement_promotes_and_moves_pointer(self):
        decision = self.decide()
        self.assertTrue(decision.promoted)
        champion = self.registry.champion("decision", SCOPE)
        self.assertEqual(champion.sha256, self.artifact.challenger.sha256)
        self.assertEqual(decision.champion_version, self.artifact.challenger.version)

    def test_no_improvement_is_rejected_and_pointer_stays(self):
        decision = self.decide(target_candidate=0.51)
        self.assertEqual(decision.reasons, ("no_target_improvement",))
        self.assertEqual(self.registry.champion("decision", SCOPE).sha256, DECISION.sha256)

    def test_boundary_gain_is_not_lost_to_float_error(self):
        # 0.55-0.53は浮動小数で0.01999...になるが、基準0.02ちょうどは昇格する。
        decision = self.decide(target_baseline=0.53, target_candidate=0.55)
        self.assertTrue(decision.promoted)

    def test_regression_is_rejected_even_with_big_gain(self):
        decision = self.decide(target_candidate=0.99, regression_candidate=0.89)
        self.assertFalse(decision.promoted)
        self.assertEqual(decision.reasons, ("regression",))

    def test_too_few_cases_are_rejected(self):
        decision = self.decide(target_cases=3, regression_cases=3)
        self.assertEqual(
            decision.reasons,
            ("insufficient_regression_cases", "insufficient_target_cases"),
        )

    def test_resource_limit_is_rejected(self):
        criteria = PromotionCriteria(max_parameter_bytes=10)
        decision = self.registry.decide(
            self.artifact, evaluation_for(self.artifact), criteria, at=LATER
        )
        self.assertEqual(decision.reasons, ("resource_exceeded",))

    def test_evaluation_of_other_artifact_is_rejected(self):
        decision = self.decide(challenger_sha256="0" * 64)
        self.assertEqual(decision.reasons, ("evaluation_mismatch",))
        decision = self.decide(artifact_id="challenger.other")
        self.assertEqual(decision.reasons, ("evaluation_mismatch",))

    def test_stale_champion_is_rejected_after_another_promotion(self):
        self.assertTrue(self.decide().promoted)
        decision = self.decide()
        self.assertEqual(decision.reasons, ("stale_champion",))

    def test_rollback_restores_previous_champion(self):
        self.decide()
        decision = self.registry.rollback(
            "decision", SCOPE, reason="regression_detected", at=LATER
        )
        self.assertEqual(decision.champion_version, DECISION.version)
        self.assertEqual(self.registry.champion("decision", SCOPE).sha256, DECISION.sha256)
        with self.assertRaises(PromotionError) as context:
            self.registry.rollback("decision", SCOPE, reason="safety_stop", at=LATER)
        self.assertEqual(context.exception.code, "no_rollback_target")

    def test_rollback_reason_is_a_fixed_code(self):
        self.decide()
        with self.assertRaises(PromotionError) as context:
            self.registry.rollback("decision", SCOPE, reason="because", at=LATER)
        self.assertEqual(context.exception.code, "invalid_input")

    def test_audit_records_every_outcome_with_chain(self):
        self.decide(target_candidate=0.5)
        self.decide()
        self.registry.rollback("decision", SCOPE, reason="operator_request", at=LATER)
        events = self.registry.audit_events()
        self.assertEqual(
            [event["action"] for event in events],
            ["initialize", "reject", "promote", "rollback"],
        )
        self.assertEqual(events[2]["artifact_id"], self.artifact.artifact_id)
        self.assertEqual(events[2]["from_sha256"], DECISION.sha256)
        self.assertEqual(events[2]["evaluation_ref"], "eval.one")
        for earlier, later in zip(events, events[1:]):
            self.assertEqual(later["prev_digest"], earlier["digest"])

    def test_targets_are_isolated_by_component_and_scope(self):
        self.registry.initialize(RECOGNITION, at=TIME)
        other = replace(DECISION, scope=OTHER_SCOPE)
        self.registry.initialize(other, at=TIME)
        self.decide()
        self.assertEqual(self.registry.champion("recognition", SCOPE).sha256, RECOGNITION.sha256)
        self.assertEqual(self.registry.champion("decision", OTHER_SCOPE).sha256, other.sha256)

    def test_uninitialized_and_duplicate_initialize_fail(self):
        with self.assertRaises(PromotionError) as context:
            self.registry.initialize(DECISION, at=TIME)
        self.assertEqual(context.exception.code, "already_initialized")
        fresh = ChampionRegistry(Path(self.directory.name) / "other.json")
        with self.assertRaises(PromotionError) as context:
            fresh.decide(self.artifact, evaluation_for(self.artifact), CRITERIA, at=LATER)
        self.assertEqual(context.exception.code, "not_initialized")

    def test_state_survives_new_registry_instance(self):
        self.decide()
        reopened = ChampionRegistry(self.path)
        self.assertEqual(
            reopened.champion("decision", SCOPE).sha256, self.artifact.challenger.sha256
        )

    def test_tampered_state_is_rejected(self):
        self.decide()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["audit"][1]["to_sha256"] = "f" * 64
        self.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(PromotionError) as context:
            self.registry.champion("decision", SCOPE)
        self.assertEqual(context.exception.code, "invalid_state")

    def test_swapped_pointer_is_rejected(self):
        self.decide()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        key = next(iter(state["targets"]))
        state["targets"][key]["champion"] = DECISION.to_dict()
        self.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(PromotionError) as context:
            self.registry.champion("decision", SCOPE)
        self.assertEqual(context.exception.code, "invalid_state")

    def test_duplicate_key_and_nan_state_are_rejected(self):
        # 有効な状態を基に、重複キー・NaNだけを注入する（他の検査では拒否されない形にする）。
        self.decide()
        valid = self.path.read_text(encoding="utf-8")
        generation = re.search(r'"generation":\s*(\d+)', valid)
        self.assertIsNotNone(generation)
        injected = {
            "duplicate": valid.replace(
                generation.group(0), generation.group(0) + ", " + generation.group(0), 1
            ),
            "nan": valid.replace(generation.group(0), '"generation": NaN', 1),
        }
        for name, text in injected.items():
            with self.subTest(name=name):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(PromotionError) as context:
                    self.registry.champion("decision", SCOPE)
                self.assertEqual(context.exception.code, "invalid_state")
        self.path.write_text(valid, encoding="utf-8")
        self.assertIsNotNone(self.registry.champion("decision", SCOPE))

    def test_used_evaluation_cannot_repromote_after_rollback(self):
        self.assertTrue(self.decide().promoted)
        self.registry.rollback("decision", SCOPE, reason="regression_detected", at=LATER)
        self.assertEqual(self.decide().reasons, ("evaluation_reused",))
        fresh = self.decide(evaluation_ref="eval.two")
        self.assertTrue(fresh.promoted)

    def test_swapped_rollback_history_is_rejected(self):
        self.decide()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        key = next(iter(state["targets"]))
        state["targets"][key]["history"][-1] = replace(DECISION, version="decision.v9").to_dict()
        self.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(PromotionError) as context:
            self.registry.rollback("decision", SCOPE, reason="safety_stop", at=LATER)
        self.assertEqual(context.exception.code, "invalid_state")

    def test_non_list_history_and_empty_audit_are_rejected(self):
        for mutate in (
            lambda state: state["targets"][next(iter(state["targets"]))].update(history={}),
            lambda state: state.update(audit=[]),
        ):
            with self.subTest(mutate=mutate):
                state = json.loads(self.path.read_text(encoding="utf-8"))
                mutate(state)
                self.path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaises(PromotionError) as context:
                    self.registry.champion("decision", SCOPE)
                self.assertEqual(context.exception.code, "invalid_state")
                self.setUp()

    def test_huge_integer_ratio_is_rejected_as_invalid_input(self):
        with self.assertRaises(PromotionError) as context:
            evaluation_for(self.artifact, target_candidate=10**1000)
        self.assertEqual(context.exception.code, "invalid_input")

    def test_oversized_state_is_not_published(self):
        with unittest.mock.patch("ai_game_player.challenger_promotion.MAX_STATE_BYTES", 10):
            with self.assertRaises(PromotionError) as context:
                self.decide()
        self.assertEqual(context.exception.code, "invalid_state")
        self.assertEqual(self.registry.champion("decision", SCOPE).sha256, DECISION.sha256)

    def test_bad_inputs_are_rejected(self):
        with self.assertRaises(PromotionError):
            evaluation_for(self.artifact, target_candidate=float("nan"))
        with self.assertRaises(PromotionError):
            evaluation_for(self.artifact, target_cases=-1)
        with self.assertRaises(PromotionError):
            PromotionCriteria(min_target_gain=0)
        with self.assertRaises(PromotionError):
            self.registry.decide(self.artifact, evaluation_for(self.artifact), CRITERIA, at="yesterday")

    def test_audit_is_bounded_and_chain_stays_valid(self):
        for _ in range(MAX_AUDIT_EVENTS + 5):
            self.decide(target_candidate=0.5)
        events = ChampionRegistry(self.path).audit_events()
        self.assertEqual(len(events), MAX_AUDIT_EVENTS)
        self.assertEqual(events[0]["sequence"], 7)


if __name__ == "__main__":
    unittest.main()
