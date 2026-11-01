from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
import tracemalloc
from unittest.mock import patch

from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.knowledge_evaluation import (
    KnowledgeEvaluationCase,
    KnowledgeEvaluationRunner,
)
from ai_game_player.knowledge_promotion import KnowledgePromotionGate
from ai_game_player.knowledge_promotion_policy import KnowledgePromotionPolicy
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.knowledge_snapshot import KnowledgeSnapshot
from ai_game_player.knowledge_snapshot_store import KnowledgeSnapshotStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation


FIXTURE = Path(__file__).parent / "fixtures/knowledge/snapshot-v1.json"


def snapshot(version, good=False, **updates):
    value = json.loads(FIXTURE.read_text())
    value["version"] = str(version)
    value["snapshot_id"] = f"knowledge.{version}"
    value["candidate_priors"] = {
        "score-label": -0.8 if good else 0.8,
        "useful-control": 0.8 if good else -0.8,
    }
    value.update(updates)
    return KnowledgeSnapshot.from_dict(value)


def provider_factory(knowledge):
    return BaselineDecisionProvider(
        min_margin=0,
        knowledge_snapshot={
            "version": knowledge.version,
            "priors": knowledge.to_dict()["candidate_priors"],
        },
    )


def cases(scope, count=12):
    observation = ScreenObservation(
        "game", 100, 80, features={"signature": "frame.original"}
    )
    candidates = [
        ActionCandidate("score-label", "wait", "Score", confidence=0.9),
        ActionCandidate("useful-control", "wait", "Continue", confidence=0.9),
        ActionCandidate("unsafe-control", "wait", "Unsafe", confidence=0.99),
    ]
    context = DecisionContextBuilder().build(observation, candidates, candidates[:2])
    groups = ("replay", "regression", "safety", "playability")
    return tuple(
        KnowledgeEvaluationCase(
            f"case.{index}",
            scope,
            groups[index % 4],
            context,
            (("score-label", -1.0), ("useful-control", 1.0)),
            sha256(f"evidence.{index}".encode()).hexdigest(),
        )
        for index in range(count)
    )


class KnowledgePromotionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = KnowledgeSnapshotStore(self.root / "snapshots")
        self.gate = KnowledgePromotionGate(self.store, self.root / "promotion")
        self.old = snapshot("old")
        self.new = snapshot("new", True)
        self.scope = self.old.scope
        self.store.save(self.old)
        self.store.save(self.new)
        self.cases = cases(self.scope)

    def tearDown(self):
        self.temporary.cleanup()

    def evaluate_for_test(self, *args, **kwargs):
        # Functional state-transition tests do not assert scheduler timing ratios.
        # Absolute resource limits and explicit over-budget rejection stay active.
        if len(args) < 5 and "policy" not in kwargs:
            kwargs["policy"] = KnowledgePromotionPolicy(
                max_latency_ratio=1000, max_memory_ratio=1000
            )
        return self.gate.evaluate_and_promote(*args, **kwargs)

    def initialize(self):
        result = self.gate.initialize_champion(self.scope, "old")
        self.assertEqual(result.status, "initialized")

    def test_actual_paired_decisions_promote_then_rollback(self):
        self.assertIsNone(self.gate.capture(self.scope).snapshot)
        self.initialize()
        captured_step = self.gate.capture(self.scope).snapshot
        result = self.evaluate_for_test(self.scope, "new", self.cases, provider_factory)
        self.assertEqual((result.status, result.reason), ("promoted", "accepted"))
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "new")
        self.assertEqual(captured_step.version, "old")
        self.assertEqual(captured_step.sha256, self.old.sha256)
        report_files = tuple((self.root / "promotion/reports").glob("*/*/payload"))
        report = json.loads(report_files[0].read_text())
        self.assertEqual(report["champion_sha256"], self.old.sha256)
        self.assertEqual(report["challenger_sha256"], self.new.sha256)
        self.assertNotIn("passed", report)
        self.assertEqual(report["policy"]["schema_version"], 1)
        self.assertEqual(len(report["measurement_sha256"]), 64)
        self.assertEqual(report["verification"], "offline_paired_decision_replay")
        self.assertEqual(report["invalid_decisions"], [0, 0])
        self.assertGreater(
            report["group_quality"]["replay"]["challenger"],
            report["group_quality"]["replay"]["champion"],
        )
        self.assertEqual(self.gate.rollback(self.scope).status, "rolled_back")
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")
        self.assertEqual(
            [entry["status"] for entry in self.gate.audit(self.scope)],
            ["initialized", "promoted", "rolled_back"],
        )

    def test_bad_knowledge_is_rejected_without_changing_champion(self):
        self.gate.initialize_champion(self.scope, "new")
        before = self.gate.capture(self.scope).snapshot
        result = self.evaluate_for_test(self.scope, "old", self.cases, provider_factory)
        self.assertEqual(
            (result.status, result.reason), ("rejected", "quality_regression")
        )
        self.assertEqual(self.gate.capture(self.scope).snapshot, before)
        self.assertEqual(
            self.gate.audit(self.scope)[-1]["reason"], "quality_regression"
        )

    def test_new_held_out_regression_can_restore_last_valid_snapshot(self):
        self.initialize()
        self.assertEqual(
            self.evaluate_for_test(
                self.scope, "new", self.cases, provider_factory
            ).status,
            "promoted",
        )
        held_out = tuple(
            replace(
                case,
                reward_by_action=(("score-label", 1.0), ("useful-control", -1.0)),
                evidence_sha256=sha256(f"heldout.{index}".encode()).hexdigest(),
            )
            for index, case in enumerate(self.cases)
        )
        report = KnowledgeEvaluationRunner().run(
            self.old, self.new, held_out, provider_factory
        )
        self.assertEqual(
            KnowledgePromotionPolicy().assess(self.old, self.new, report),
            "quality_regression",
        )
        self.assertEqual(self.gate.rollback(self.scope, "old").status, "rolled_back")
        self.assertEqual(self.gate.capture(self.scope).snapshot.sha256, self.old.sha256)

    def test_low_sample_uncertainty_missing_groups_duplicate_cases_and_resource_budget(
        self,
    ):
        self.initialize()
        uncertain = snapshot("uncertain", True, uncertainty=1.0)
        self.store.save(uncertain)
        result = self.evaluate_for_test(
            self.scope, "uncertain", self.cases, provider_factory
        )
        self.assertEqual(result.reason, "insufficient_knowledge")
        result = self.evaluate_for_test(
            self.scope, "new", self.cases[:2], provider_factory
        )
        self.assertEqual(result.reason, "insufficient_cases")
        result = self.evaluate_for_test(
            self.scope, "new", (self.cases[0],) * 12, provider_factory
        )
        self.assertEqual(result.reason, "invalid_evaluation")
        relabeled_duplicates = tuple(
            replace(case, evidence_sha256=self.cases[0].evidence_sha256)
            for case in self.cases
        )
        result = self.evaluate_for_test(
            self.scope, "new", relabeled_duplicates, provider_factory
        )
        self.assertEqual(result.reason, "invalid_evaluation")
        only_replay = tuple(replace(case, group="replay") for case in self.cases)
        result = self.evaluate_for_test(
            self.scope, "new", only_replay, provider_factory
        )
        self.assertEqual(result.reason, "missing_evaluation_group")
        result = self.evaluate_for_test(
            self.scope,
            "new",
            self.cases,
            provider_factory,
            KnowledgePromotionPolicy(max_python_bytes=1),
        )
        self.assertEqual(result.reason, "memory_budget_exceeded")
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")

    def test_unsafe_provider_or_exception_cannot_be_promoted(self):
        self.initialize()

        class UnsafeProvider:
            def choose_context(self, context):
                return ActionDecision(
                    "unsafe-control",
                    "choose action",
                    "test",
                    context.snapshot_id,
                    context.state["screen_id"],
                    context.state["signature"],
                )

        def factory(knowledge):
            return (
                UnsafeProvider()
                if knowledge.version == "new"
                else provider_factory(knowledge)
            )

        result = self.evaluate_for_test(self.scope, "new", self.cases, factory)
        self.assertEqual(result.reason, "unsafe_or_failed_decision")

        def broken_factory(knowledge):
            if knowledge.version == "new":
                raise RuntimeError("provider initialization failed")
            return provider_factory(knowledge)

        result = self.evaluate_for_test(self.scope, "new", self.cases, broken_factory)
        self.assertEqual(result.reason, "unsafe_or_failed_decision")
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")

    def test_broken_baseline_does_not_manufacture_target_improvement(self):
        self.initialize()

        def broken_baseline(knowledge):
            if knowledge.version == "old":
                raise RuntimeError("baseline model unavailable")
            return provider_factory(knowledge)

        result = self.evaluate_for_test(self.scope, "new", self.cases, broken_baseline)
        self.assertEqual(result.reason, "invalid_baseline_comparison")
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")

    def test_other_scope_expired_version_and_unknown_rollback_are_rejected(self):
        self.initialize()
        other = KnowledgeScope(
            "other-game",
            self.scope.scene_id,
            self.scope.profile_id,
            self.scope.player_id,
            self.scope.lineage_id,
        )
        result = self.evaluate_for_test(other, "new", self.cases, provider_factory)
        self.assertEqual(result.reason, "missing_champion")
        alternate = tuple(replace(case, scope=other) for case in self.cases)
        result = self.evaluate_for_test(self.scope, "new", alternate, provider_factory)
        self.assertEqual(result.reason, "invalid_evaluation")
        expired = snapshot("expired", True, expires_at="2026-11-01T00:00:01Z")
        self.store.save(expired)
        result = self.evaluate_for_test(
            self.scope, "expired", self.cases, provider_factory
        )
        self.assertEqual(result.reason, "invalid_challenger")
        self.assertEqual(
            self.gate.rollback(self.scope, "unseen").reason,
            "rollback_version_unavailable",
        )
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")

    def test_failure_during_atomic_publish_preserves_previous_pointer_and_audit(self):
        self.initialize()
        before = (self.root / "promotion" / self.scope.key / "state.json").read_bytes()
        with patch("ai_game_player.knowledge_promotion.stage_json_write") as stage:
            stage.return_value.publish.side_effect = OSError(
                "simulated crash before replace"
            )
            with self.assertRaises(OSError):
                self.evaluate_for_test(self.scope, "new", self.cases, provider_factory)
        self.assertEqual(
            (self.root / "promotion" / self.scope.key / "state.json").read_bytes(),
            before,
        )
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")

    def test_generation_change_during_evaluation_rejects_stale_result(self):
        self.initialize()
        mutated = False

        def factory(knowledge):
            nonlocal mutated
            if not mutated:
                mutated = True
                # A concurrent update records another accepted control operation.
                self.gate.rollback(self.scope, "unseen")
            return provider_factory(knowledge)

        result = self.evaluate_for_test(self.scope, "new", self.cases, factory)
        self.assertEqual(result.reason, "stale_evaluation")
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")

    def test_corrupt_or_oversized_state_falls_back_without_overwriting_evidence(self):
        self.initialize()
        path = self.root / "promotion" / self.scope.key / "state.json"
        valid = path.read_bytes()
        for corrupt in (b'{"truncated":', b"x" * (128 * 1024 + 1)):
            path.write_bytes(corrupt)
            self.assertIsNone(self.gate.capture(self.scope).snapshot)
            with self.assertRaises(ValueError):
                self.gate.initialize_champion(self.scope, "old")
            self.assertEqual(path.read_bytes(), corrupt)
        path.write_bytes(valid)
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "old")

    def test_tampered_previous_snapshot_cannot_be_rollback_target(self):
        self.initialize()
        self.evaluate_for_test(self.scope, "new", self.cases, provider_factory)
        payload = (
            self.store.root
            / "objects"
            / self.old.sha256[:2]
            / self.old.sha256
            / "payload"
        )
        payload.write_bytes(b"tampered")
        result = self.gate.rollback(self.scope, "old")
        self.assertEqual(result.reason, "invalid_rollback_snapshot")
        self.assertEqual(self.gate.capture(self.scope).snapshot.version, "new")

    def test_replay_cannot_modify_source_context_or_supply_a_pass_boolean(self):
        original = deepcopy(self.cases[0].context.to_dict())

        def mutation_factory(knowledge):
            class MutatingProvider:
                def choose_context(self, context):
                    context.state["signature"] = "tampered"
                    return provider_factory(knowledge).choose_context(context)

            return MutatingProvider()

        report = KnowledgeEvaluationRunner().run(
            self.old, self.new, self.cases, mutation_factory
        )
        self.assertEqual(report.invalid_decisions, (12, 12))
        self.assertEqual(self.cases[0].context.to_dict(), original)
        with self.assertRaises(TypeError):
            self.evaluate_for_test(self.scope, "new", passed=True)

    def test_policy_cannot_relax_safety_or_accept_nonfinite_budgets(self):
        for kwargs in (
            {"min_cases": 1},
            {"min_samples": 1},
            {"max_latency_ms": float("inf")},
            {"max_uncertainty": 2},
            {"max_python_bytes": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                KnowledgePromotionPolicy(**kwargs)

    def test_memory_measurements_do_not_overlap_or_stop_an_external_trace(self):
        from ai_game_player.knowledge_evaluation import _EVALUATION_TRACE_LOCK

        runner = KnowledgeEvaluationRunner()
        with _EVALUATION_TRACE_LOCK:
            with self.assertRaises(ValueError):
                runner.run(self.old, self.new, self.cases, provider_factory)
        tracemalloc.start()
        try:
            with self.assertRaises(ValueError):
                runner.run(self.old, self.new, self.cases, provider_factory)
            self.assertTrue(tracemalloc.is_tracing())
        finally:
            tracemalloc.stop()

    def test_control_audit_and_rollback_history_are_bounded(self):
        self.initialize()
        with (
            patch("ai_game_player.knowledge_promotion.MAX_AUDIT_EVENTS", 3),
            patch("ai_game_player.knowledge_promotion.MAX_ROLLBACK_VERSIONS", 2),
        ):
            for _ in range(4):
                self.assertEqual(
                    self.evaluate_for_test(
                        self.scope, "new", self.cases, provider_factory
                    ).status,
                    "promoted",
                )
                self.assertEqual(
                    self.gate.rollback(self.scope, "old").status, "rolled_back"
                )
            self.assertEqual(len(self.gate.audit(self.scope)), 3)
            state = json.loads(
                (self.root / "promotion" / self.scope.key / "state.json").read_text()
            )
            self.assertEqual(len(state["history"]), 2)
            self.assertEqual(state["audit"][-1]["generation"], state["generation"])


if __name__ == "__main__":
    unittest.main()
