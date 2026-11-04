import json
import random
import unittest
from dataclasses import replace

from ai_game_player.experience import (
    ArtifactReference,
    ExperienceEpisode,
    ExperienceEvent,
    ExperienceProvenance,
    ExperienceStep,
)
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.learning_dataset import DatasetSource
from ai_game_player.training_diagnosis import (
    CONTEXT_EVENT_TYPE,
    collect_diagnosis_cases,
    diagnose_case,
    diagnose_cases,
    diagnose_episodes,
)
from ai_game_player.training_diagnosis_models import (
    ComponentAttribution,
    ComponentTarget,
    DiagnosisCase,
)
from ai_game_player.training_diagnosis_policy import (
    TrainingDiagnosisPolicy,
    weaker_label_status,
)


SCOPE = KnowledgeScope("test-game", "playing", "default", "player.one", "lineage.one")
OTHER_SCOPE = replace(SCOPE, scene_id="menu")
TIME = "2026-11-04T12:00:00Z"
POLICY = TrainingDiagnosisPolicy()


def outcome_event(
    number,
    outcome="failure",
    *,
    evidence="deterministic_action_effect",
    verified=True,
    confidence=1.0,
    execution_status="executed",
    scope=SCOPE,
    reliability=None,
    artifacts=(),
):
    payload = {
        "scope": scope.to_dict(),
        "source_id": "session.one",
        "source_sequence": number,
        "correlation_id": "session.one",
        "correlation_sequence": number,
        "action_generation": number,
        "execution_status": execution_status,
        "action_id": "action.press",
        "outcome": outcome,
        "evidence_kind": evidence,
        "verified": verified,
        "confidence": confidence,
    }
    if reliability is not None:
        payload["candidate_reliability"] = reliability
    return ExperienceEvent(
        f"effect.{number}",
        "knowledge.action_outcome",
        TIME,
        "light_execution_projection",
        payload,
        artifacts=artifacts,
        provenance=ExperienceProvenance(
            evidence, confidence=confidence, evidence_event_id=f"effect.{number}"
        ),
    )


def context_event(
    number,
    target=None,
    *,
    label="verified_effect",
    expected=None,
    reliability=None,
    conflict=False,
    abstained=False,
):
    return ExperienceEvent(
        f"context.{number}",
        CONTEXT_EVENT_TYPE,
        TIME,
        "diagnosis_test",
        {
            "outcome_event_id": target or f"effect.{number}",
            "label_status": label,
            "recognition_reliability": reliability,
            "decision_expected_success": expected,
            "outcome_conflict": conflict,
            "outcome_abstained": abstained,
        },
    )


def episode(*steps, episode_id="episode.one"):
    return ExperienceEpisode(
        episode_id,
        TIME,
        "diagnosis_test",
        tuple(
            ExperienceStep(
                f"step.{index}", index, TIME, "diagnosis_test", tuple(events)
            )
            for index, events in enumerate(steps)
        ),
    )


def case(number=1, **overrides):
    values = {
        "ref": DatasetSource("episode.one", f"step.{number}", f"effect.{number}"),
        "scope": SCOPE,
        "outcome": "failure",
        "outcome_confidence": 1.0,
        "execution_status": "executed",
        "label_status": "verified_effect",
    }
    values.update(overrides)
    return DiagnosisCase(**values)


def target(report, component, scope=SCOPE):
    for item in report.targets:
        if item.component == component and item.scope == scope:
            return item
    return None


class TrainingDiagnosisTest(unittest.TestCase):
    def test_verified_failures_select_decision_candidate_with_evidence(self):
        report = diagnose_episodes(
            [
                episode(
                    [outcome_event(1), context_event(1, expected=0.9)],
                    [outcome_event(2), context_event(2, expected=0.8)],
                )
            ]
        )
        decision = target(report, "decision")
        self.assertEqual(decision.status, "candidate")
        self.assertEqual(decision.strict_support, 2)
        self.assertGreater(decision.confidence, POLICY.uncertain_confidence_cap)
        self.assertEqual(decision.failure_kinds, (("decision_overconfident", 2),))
        self.assertEqual(
            [ref.event_id for ref in decision.evidence_refs], ["effect.1", "effect.2"]
        )
        self.assertTrue(all(item.attributions[0].strict for item in report.findings))
        self.assertEqual(report.findings[0].ref.event_id, "effect.1")

    def test_uncertain_labels_never_claim_certainty(self):
        steps = [
            [outcome_event(n, verified=False, evidence="execution_observation")]
            for n in range(20)
        ]
        report = diagnose_episodes([episode(*steps)])
        self.assertTrue(report.targets)
        for item in report.targets:
            self.assertEqual(item.status, "hypothesis")
            self.assertEqual(item.strict_support, 0)
            self.assertLessEqual(item.confidence, POLICY.uncertain_confidence_cap)
        for finding in report.findings:
            self.assertEqual(finding.label_status, "observed_only")
            for attribution in finding.attributions:
                self.assertFalse(attribution.strict)
                self.assertEqual(attribution.basis, "uncertain_label")
                self.assertLessEqual(
                    attribution.confidence, POLICY.uncertain_confidence_cap
                )

    def test_single_strict_support_stays_hypothesis(self):
        report = diagnose_cases([case(1)], POLICY)
        decision = target(report, "decision")
        self.assertEqual(decision.status, "hypothesis")
        self.assertEqual(decision.strict_support, 1)
        self.assertLessEqual(decision.confidence, POLICY.uncertain_confidence_cap)

    def test_context_cannot_upgrade_but_can_downgrade_label(self):
        cases, _ = collect_diagnosis_cases(
            [
                episode(
                    [
                        outcome_event(
                            1, verified=False, evidence="execution_observation"
                        ),
                        context_event(1, label="verified_effect"),
                    ],
                    [outcome_event(2), context_event(2, label="expected_consistent")],
                )
            ],
            POLICY,
        )
        self.assertEqual(
            [item.label_status for item in cases],
            ["observed_only", "expected_consistent"],
        )
        self.assertEqual(weaker_label_status("verified_effect", "conflict"), "conflict")

    def test_calibrated_effect_is_supported_not_verified(self):
        cases, _ = collect_diagnosis_cases(
            [episode([outcome_event(1, evidence="calibrated_action_effect")])], POLICY
        )
        self.assertEqual(cases[0].label_status, "supported_effect")
        finding = diagnose_case(cases[0], POLICY)
        self.assertFalse(any(item.strict for item in finding.attributions))

    def test_mismatched_provenance_is_not_verified(self):
        event = outcome_event(1)
        event = replace(
            event,
            provenance=ExperienceProvenance(
                "human_correction", confidence=1.0, evidence_event_id="effect.1"
            ),
        )
        cases, _ = collect_diagnosis_cases([episode([event])], POLICY)
        self.assertEqual(cases[0].label_status, "observed_only")

    def test_execution_failures_are_attributed_to_execution_only(self):
        failed = diagnose_case(case(execution_status="before_dispatch_failure"), POLICY)
        self.assertEqual(
            [
                (a.component, a.failure_kind, a.strict, a.basis)
                for a in failed.attributions
            ],
            [("execution", "execution_failure", True, "receipt")],
        )
        unknown = diagnose_case(
            case(execution_status="dispatch_unknown", decision_expected_success=0.9),
            POLICY,
        )
        self.assertEqual(
            [(a.component, a.strict) for a in unknown.attributions],
            [("execution", False)],
        )

    def test_conflict_and_unresolved_outcome_target_interpretation(self):
        conflict = diagnose_case(case(outcome_conflict=True), POLICY)
        self.assertEqual(
            [(a.component, a.failure_kind) for a in conflict.attributions],
            [("outcome_interpretation", "outcome_conflict")],
        )
        self.assertFalse(conflict.attributions[0].strict)
        abstained = diagnose_case(
            case(outcome="unknown", outcome_abstained=True), POLICY
        )
        plain = diagnose_case(case(outcome="unknown", label_status="unknown"), POLICY)
        self.assertGreater(abstained.training_value, plain.training_value)
        self.assertEqual(abstained.attributions[0].failure_kind, "outcome_unresolved")

    def test_low_recognition_reliability_shifts_blame_to_recognition(self):
        reliable = diagnose_case(case(recognition_reliability=0.95), POLICY)
        unreliable = diagnose_case(case(recognition_reliability=0.1), POLICY)
        self.assertEqual([a.component for a in reliable.attributions], ["decision"])
        components = {a.component: a.confidence for a in unreliable.attributions}
        self.assertGreater(components["recognition"], components["decision"])
        decision_reliable = reliable.attributions[0].confidence
        self.assertLess(components["decision"], decision_reliable)

    def test_context_and_sample_reliability_use_lower_value(self):
        cases, _ = collect_diagnosis_cases(
            [
                episode(
                    [
                        outcome_event(1, reliability=0.8),
                        context_event(1, reliability=0.2),
                    ]
                )
            ],
            POLICY,
        )
        self.assertEqual(cases[0].recognition_reliability, 0.2)

    def test_underestimated_success_and_plain_success(self):
        underestimated = diagnose_case(
            case(outcome="success", decision_expected_success=0.1), POLICY
        )
        self.assertEqual(
            underestimated.attributions[0].failure_kind, "decision_underestimated"
        )
        plain = diagnose_case(case(outcome="success"), POLICY)
        self.assertEqual(plain.training_value, 0)
        self.assertEqual(plain.attributions, ())
        self.assertEqual(diagnose_cases([case(outcome="success")], POLICY).findings, ())

    def test_exclusions_are_counted_not_diagnosed(self):
        invalid = ExperienceEvent(
            "effect.9", "knowledge.action_outcome", TIME, "test", {"outcome": "failure"}
        )
        bad_context = ExperienceEvent(
            "context.bad",
            CONTEXT_EVENT_TYPE,
            TIME,
            "test",
            {"label_status": "verified_effect"},
        )
        report = diagnose_episodes(
            [
                episode(
                    [outcome_event(1, execution_status="dry_run")],
                    [invalid, bad_context],
                    [context_event(3, target="effect.missing")],
                    [
                        outcome_event(4),
                        context_event(4),
                        context_event(5, target="effect.4"),
                    ],
                )
            ]
        )
        self.assertEqual(
            dict(report.excluded),
            {
                "invalid_context": 3,
                "invalid_outcome_event": 1,
                "not_executed": 1,
                "orphan_context": 1,
            },
        )
        self.assertEqual(report.cases_considered, 1)

    def test_non_string_context_label_is_counted_invalid(self):
        bad = replace(
            context_event(1),
            payload={**context_event(1).payload, "label_status": ["verified_effect"]},
        )
        cases, excluded = collect_diagnosis_cases(
            [episode([outcome_event(1, verified=False), bad])], POLICY
        )
        self.assertEqual(excluded, {"invalid_context": 1})
        self.assertEqual(cases[0].label_status, "observed_only")

    def test_duplicate_episode_and_case_limit(self):
        first = episode([outcome_event(1)], [outcome_event(2)])
        policy = TrainingDiagnosisPolicy(max_cases=1)
        cases, excluded = collect_diagnosis_cases([first, first], policy)
        self.assertEqual(len(cases), 1)
        self.assertEqual(excluded, {"case_limit": 2, "duplicate_reference": 1})
        with self.assertRaises(ValueError):
            diagnose_cases([case(1), case(2)], policy)

    def test_report_is_deterministic_and_order_independent(self):
        episodes = [
            episode(
                [outcome_event(n), context_event(n, expected=0.9)],
                episode_id=f"episode.{n}",
            )
            for n in range(6)
        ]
        first = diagnose_episodes(episodes)
        shuffled = list(episodes)
        random.Random(7).shuffle(shuffled)
        second = diagnose_episodes(shuffled)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertTrue(first.diagnosis_id.startswith("diagnosis."))
        json.dumps(first.to_dict(), allow_nan=False)
        changed = diagnose_episodes(
            episodes, TrainingDiagnosisPolicy(minimum_strict_support=3)
        )
        self.assertNotEqual(first.policy_fingerprint, changed.policy_fingerprint)
        self.assertNotEqual(first.diagnosis_id, changed.diagnosis_id)

    def test_output_is_bounded(self):
        policy = TrainingDiagnosisPolicy(
            max_findings=3, max_targets=1, max_evidence_refs=2
        )
        cases = [case(n, recognition_reliability=0.1) for n in range(10)]
        report = diagnose_cases(cases, policy)
        self.assertEqual(len(report.findings), 3)
        self.assertTrue(report.findings_truncated)
        self.assertEqual(len(report.targets), 1)
        self.assertTrue(report.targets_truncated)
        self.assertEqual(len(report.targets[0].evidence_refs), 2)
        self.assertEqual(report.to_dict()["schema_version"], 1)

    def test_targets_are_separated_by_scope(self):
        report = diagnose_cases([case(1), case(2), case(3, scope=OTHER_SCOPE)], POLICY)
        self.assertEqual(target(report, "decision").strict_support, 2)
        self.assertEqual(target(report, "decision", OTHER_SCOPE).strict_support, 1)
        self.assertEqual(report.targets[0].status, "candidate")

    def test_training_value_is_independent_of_storage_properties(self):
        artifact = ArtifactReference("frame.one", "a" * 64, "image/png", True)
        plain, _ = collect_diagnosis_cases([episode([outcome_event(1)])], POLICY)
        stored, _ = collect_diagnosis_cases(
            [episode([outcome_event(1, artifacts=(artifact,))])], POLICY
        )
        self.assertEqual(
            diagnose_case(plain[0], POLICY).to_dict(),
            diagnose_case(stored[0], POLICY).to_dict(),
        )
        self.assertNotIn(
            "artifact", json.dumps(diagnose_cases(plain, POLICY).to_dict())
        )

    def test_contract_validation(self):
        with self.assertRaises(ValueError):
            ComponentAttribution(
                "decision", "action_ineffective", 0.4, "uncertain_label", True
            )
        with self.assertRaises(ValueError):
            ComponentTarget("decision", SCOPE, "candidate", 0.4, 0, 3, (), ())
        with self.assertRaises(ValueError):
            TrainingDiagnosisPolicy(
                observed_only_label_weight=1.0, verified_label_weight=0.5
            )
        with self.assertRaises(ValueError):
            TrainingDiagnosisPolicy(uncertain_confidence_cap=float("nan"))
        with self.assertRaises(ValueError):
            TrainingDiagnosisPolicy(max_cases=0)
        with self.assertRaises(ValueError):
            case(label_status="confirmed")
        with self.assertRaises(ValueError):
            case(recognition_reliability=1.5)
        with self.assertRaises(ValueError):
            diagnose_episodes(["not an episode"])
        with self.assertRaises(ValueError):
            diagnose_cases(["not a case"], POLICY)
        with self.assertRaises(ValueError):
            weaker_label_status("verified_effect", "certain")


if __name__ == "__main__":
    unittest.main()
