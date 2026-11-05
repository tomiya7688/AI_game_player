import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.challenger_update import (
    _apply,
    build_challenger,
    load_challenger,
    propose_updates,
    save_challenger,
)
from ai_game_player.challenger_update_models import (
    ChallengerArtifact,
    ChallengerUpdateError,
    ComponentParameters,
    Prototype,
    UpdateProposal,
)
from ai_game_player.challenger_update_policy import ChallengerUpdatePolicy
from ai_game_player.experience import (
    ExperienceEpisode,
    ExperienceEvent,
    ExperienceProvenance,
    ExperienceStep,
)
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.learning_dataset import DatasetSource
from ai_game_player.training_diagnosis import (
    CONTEXT_EVENT_TYPE,
    diagnose_cases,
    diagnose_episodes,
)
from ai_game_player.training_diagnosis_models import DiagnosisCase
from ai_game_player.training_diagnosis_policy import TrainingDiagnosisPolicy


SCOPE = KnowledgeScope("test-game", "playing", "default", "player.one", "lineage.one")
OTHER_SCOPE = replace(SCOPE, scene_id="menu")
TIME = "2026-11-05T01:00:00Z"
DECISION = ComponentParameters("decision", SCOPE, "decision.v1")
RECOGNITION = ComponentParameters("recognition", SCOPE, "recognition.v1")
CHAMPIONS = (DECISION, RECOGNITION)


def case(number, **overrides):
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


def overconfident(*numbers, **overrides):
    return [case(n, decision_expected_success=0.9, **overrides) for n in numbers]


def report_of(cases, policy=None):
    return diagnose_cases(cases, policy or TrainingDiagnosisPolicy())


def proposals_for(cases, champions=CHAMPIONS, policy=None, diagnosis=None):
    return propose_updates(report_of(cases, diagnosis), champions, policy)


def code_of(context):
    return context.exception.code


class ProposalTest(unittest.TestCase):
    def test_candidate_target_produces_typed_proposal(self):
        result = proposals_for(overconfident(1, 2, 3))
        first = result.proposals[0]
        self.assertEqual(
            (first.component, first.strategy, first.parameter, first.direction),
            ("decision", "calibration", "confidence_scale", "decrease"),
        )
        self.assertEqual(first.failure_kind, "decision_overconfident")
        self.assertEqual(first.strict_support, 3)
        self.assertEqual(first.magnitude, round(0.11 * first.target_confidence, 6))
        self.assertEqual(first.champion_sha256, DECISION.sha256)
        self.assertEqual(first.rank, 0)
        self.assertEqual(
            [p.strategy for p in result.proposals], ["calibration", "prototype"]
        )
        self.assertEqual(result.to_dict()["schema"], "challenger-update-proposals")

    def test_proposal_round_trips_and_id_is_content_digest(self):
        proposal = proposals_for(overconfident(1, 2)).proposals[0]
        data = proposal.to_dict()
        self.assertEqual(UpdateProposal.from_dict(data), proposal)
        self.assertTrue(data["proposal_id"].startswith("proposal."))
        forged = {**data, "magnitude": 0.01}
        with self.assertRaises(ChallengerUpdateError) as context:
            UpdateProposal.from_dict(forged)
        self.assertEqual(code_of(context), "invalid_proposal")

    def test_strategy_table_orders_multiple_strategies(self):
        result = proposals_for([case(n) for n in (1, 2)])
        self.assertEqual(
            [(p.failure_kind, p.strategy, p.rank) for p in result.proposals],
            [
                ("action_ineffective", "prototype", 0),
                ("action_ineffective", "calibration", 1),
            ],
        )
        recognition = proposals_for(
            [case(n, recognition_reliability=0.2) for n in (1, 2)],
            policy=ChallengerUpdatePolicy(max_proposals_per_target=3),
        )
        strategies = [
            p.strategy for p in recognition.proposals if p.component == "recognition"
        ]
        self.assertEqual(strategies, ["threshold", "calibration"])

    def test_disabled_strategy_is_skipped(self):
        policy = ChallengerUpdatePolicy(enabled_strategies=("threshold",))
        result = proposals_for(overconfident(1, 2), policy=policy)
        self.assertEqual(result.proposals, ())
        self.assertEqual(
            result.skipped, (("decision", SCOPE.key, "strategy_disabled"),)
        )

    def test_intensity_scales_step_within_hard_cap(self):
        low = ChallengerUpdatePolicy(intensity=0.0)
        high = ChallengerUpdatePolicy(intensity=1.0)
        capped = ChallengerUpdatePolicy(
            intensity=1.0, calibration_min_step=0.25, calibration_max_step=0.25
        )
        self.assertAlmostEqual(low.max_step("calibration"), 0.02)
        self.assertAlmostEqual(high.max_step("calibration"), 0.2)
        self.assertLessEqual(capped.max_step("calibration"), 0.25)
        cases = overconfident(1, 2)
        small = proposals_for(cases, policy=low).proposals[0].magnitude
        large = proposals_for(cases, policy=high).proposals[0].magnitude
        self.assertLess(small, large)
        with self.assertRaises(ValueError):
            ChallengerUpdatePolicy(calibration_max_step=0.3)

    def test_hypothesis_target_becomes_observation_request(self):
        result = proposals_for(overconfident(1, 2, label_status="supported_effect"))
        self.assertEqual(result.proposals, ())
        request = result.observation_requests[0]
        self.assertEqual(request.component, "decision")
        self.assertLessEqual(request.confidence, 0.5)
        self.assertEqual(request.strict_support, 0)
        self.assertEqual(request.reason, "insufficient_strict_support")

    def test_uncertain_refs_are_not_used_as_prototypes(self):
        verified = [case(n) for n in (1, 2)]
        uncertain = [case(n, label_status="supported_effect") for n in (3, 4, 5)]
        result = proposals_for(verified + uncertain)
        prototype = next(p for p in result.proposals if p.strategy == "prototype")
        self.assertEqual(
            {ref.event_id for ref in prototype.strict_refs}, {"effect.1", "effect.2"}
        )
        self.assertEqual(prototype.strict_support, 2)

    def test_truncated_findings_fail_closed(self):
        result = proposals_for(
            overconfident(1, 2, 3), diagnosis=TrainingDiagnosisPolicy(max_findings=1)
        )
        self.assertEqual(result.proposals, ())
        self.assertEqual(
            result.skipped, (("decision", SCOPE.key, "strict_evidence_unavailable"),)
        )

    def test_strict_receipt_is_not_a_verified_label(self):
        failures = [
            case(n, execution_status="before_dispatch_failure") for n in (1, 2, 3)
        ]
        report = report_of(failures)
        self.assertEqual(report.targets[0].status, "candidate")
        result = propose_updates(report, CHAMPIONS)
        self.assertEqual(result.proposals, ())
        self.assertEqual(
            result.skipped, (("execution", SCOPE.key, "no_applicable_strategy"),)
        )

    def test_prototype_only_for_negative_kinds(self):
        with self.assertRaises(ValueError):
            ChallengerUpdatePolicy(
                strategy_table=(("decision_underestimated", ("prototype",)),)
            )
        proposal = proposals_for(overconfident(1, 2)).proposals[0]
        with self.assertRaises(ChallengerUpdateError) as context:
            replace(
                proposal,
                failure_kind="decision_underestimated",
                strategy="prototype",
                parameter="prototypes",
                direction="add",
            )
        self.assertEqual(code_of(context), "invalid_proposal")

    def test_ambiguous_failure_kind_is_skipped(self):
        cases = overconfident(1, 2) + [case(3), case(4)]
        result = proposals_for(cases)
        self.assertEqual(
            result.skipped, (("decision", SCOPE.key, "ambiguous_failure_kind"),)
        )

    def test_missing_champion_is_skipped(self):
        result = proposals_for(overconfident(1, 2), champions=(RECOGNITION,))
        self.assertEqual(result.skipped, (("decision", SCOPE.key, "missing_champion"),))

    def test_out_of_bounds_parameter_is_rejected(self):
        low = replace(DECISION, confidence_scale=0.1)
        policy = ChallengerUpdatePolicy(enabled_strategies=("calibration",))
        result = proposals_for(overconfident(1, 2), champions=(low,), policy=policy)
        self.assertEqual(
            result.skipped, (("decision", SCOPE.key, "nothing_to_update"),)
        )
        strict = replace(RECOGNITION, acceptance_threshold=1.0)
        cases = [case(n, recognition_reliability=0.2) for n in (1, 2)]
        result = proposals_for(cases, champions=(DECISION, strict))
        strategies = [
            p.strategy for p in result.proposals if p.component == "recognition"
        ]
        self.assertEqual(strategies, ["calibration"])
        proposal = proposals_for(overconfident(1, 2)).proposals[0]
        with self.assertRaises(ChallengerUpdateError) as context:
            _apply(proposal, low, 256)
        self.assertEqual(code_of(context), "parameter_out_of_bounds")

    def test_prototype_capacity(self):
        proposal = next(
            p
            for p in proposals_for([case(n) for n in (1, 2)]).proposals
            if p.strategy == "prototype"
        )
        with self.assertRaises(ChallengerUpdateError) as context:
            _apply(proposal, DECISION, 1)
        self.assertEqual(code_of(context), "prototype_capacity")
        full = replace(
            DECISION,
            prototypes=tuple(
                Prototype(DatasetSource("episode.one", "step.x", f"event.{n:03d}"))
                for n in range(256)
            ),
        )
        result = proposals_for([case(n) for n in (1, 2)], champions=(full,))
        self.assertEqual([p.strategy for p in result.proposals], ["calibration"])
        existing = replace(
            DECISION,
            prototypes=tuple(sorted(Prototype(ref) for ref in proposal.strict_refs)),
        )
        result = proposals_for([case(n) for n in (1, 2)], champions=(existing,))
        self.assertEqual([p.strategy for p in result.proposals], ["calibration"])

    def test_policy_validation(self):
        invalid = (
            {"intensity": 1.5},
            {"intensity": True},
            {"enabled_strategies": ()},
            {"enabled_strategies": ("calibration", "calibration")},
            {"enabled_strategies": ("lightweight_ml",)},
            {"enabled_strategies": ("calibration", [])},
            {"minimum_strict_support": 1},
            {"max_proposals": 0},
            {"max_prototypes_per_proposal": 17},
            {"threshold_min_step": 0.2, "threshold_max_step": 0.1},
            {"strategy_table": (("execution_failure", ("calibration",)),)},
            {
                "strategy_table": (
                    ("recognition_unreliable", ("threshold",)),
                    ("action_ineffective", ("prototype",)),
                )
            },
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                ChallengerUpdatePolicy(**values)
        self.assertEqual(
            ChallengerUpdatePolicy().fingerprint, ChallengerUpdatePolicy().fingerprint
        )
        self.assertNotEqual(
            ChallengerUpdatePolicy().fingerprint,
            ChallengerUpdatePolicy(intensity=0.4).fingerprint,
        )
        with self.assertRaises(ChallengerUpdateError) as context:
            propose_updates(report_of(overconfident(1, 2)), CHAMPIONS, object())
        self.assertEqual(code_of(context), "invalid_policy")

    def test_proposal_limit_truncates(self):
        cases = overconfident(1, 2) + [
            case(n, recognition_reliability=0.2, scope=OTHER_SCOPE) for n in (3, 4)
        ]
        champions = CHAMPIONS + (
            replace(DECISION, scope=OTHER_SCOPE),
            replace(RECOGNITION, scope=OTHER_SCOPE),
        )
        result = proposals_for(
            cases, champions=champions, policy=ChallengerUpdatePolicy(max_proposals=1)
        )
        self.assertEqual(len(result.proposals), 1)
        self.assertTrue(result.proposals_truncated)

    def test_duplicate_targets_and_champions_are_rejected(self):
        report = report_of(overconfident(1, 2))
        with self.assertRaises(ChallengerUpdateError) as context:
            propose_updates(replace(report, targets=report.targets * 2), CHAMPIONS)
        self.assertEqual(code_of(context), "invalid_report")
        with self.assertRaises(ChallengerUpdateError) as context:
            propose_updates(report, (DECISION, DECISION))
        self.assertEqual(code_of(context), "invalid_parameters")
        with self.assertRaises(ChallengerUpdateError) as context:
            propose_updates(object(), CHAMPIONS)
        self.assertEqual(code_of(context), "invalid_report")

    def test_same_input_same_output(self):
        cases = overconfident(1, 2, 3) + [
            case(n, recognition_reliability=0.2, scope=OTHER_SCOPE) for n in (4, 5)
        ]
        champions = CHAMPIONS + (
            replace(DECISION, scope=OTHER_SCOPE),
            replace(RECOGNITION, scope=OTHER_SCOPE),
        )
        report = report_of(cases)
        shuffled = replace(
            report,
            targets=tuple(reversed(report.targets)),
            findings=tuple(reversed(report.findings)),
        )
        first = propose_updates(report, champions)
        reordered = propose_updates(report, tuple(reversed(champions)))
        self.assertEqual(reordered.proposal_set_id, first.proposal_set_id)
        # 並べ替えた報告はdiagnosis_idが変わるので、識別子以外の提案内容と順序を比べる。
        second = propose_updates(shuffled, champions)

        def shape(result):
            return [
                (p.component, p.scope, p.strategy, p.magnitude, p.strict_refs, p.rank)
                for p in result.proposals
            ]

        self.assertEqual(shape(first), shape(second))
        self.assertEqual(first.observation_requests, second.observation_requests)
        self.assertEqual(first.skipped, second.skipped)
        self.assertEqual(
            report_of(list(reversed(cases))).diagnosis_id, report.diagnosis_id
        )
        self.assertEqual(
            propose_updates(
                report_of(list(reversed(cases))), champions
            ).proposal_set_id,
            first.proposal_set_id,
        )

    def test_episodes_end_to_end(self):
        steps = []
        for number in (1, 2):
            effect = ExperienceEvent(
                f"effect.{number}",
                "knowledge.action_outcome",
                TIME,
                "light_execution_projection",
                {
                    "scope": SCOPE.to_dict(),
                    "source_id": "session.one",
                    "source_sequence": number,
                    "correlation_id": "session.one",
                    "correlation_sequence": number,
                    "action_generation": number,
                    "execution_status": "executed",
                    "action_id": "action.press",
                    "outcome": "failure",
                    "evidence_kind": "deterministic_action_effect",
                    "verified": True,
                    "confidence": 1.0,
                },
                provenance=ExperienceProvenance(
                    "deterministic_action_effect",
                    confidence=1.0,
                    evidence_event_id=f"effect.{number}",
                ),
            )
            context = ExperienceEvent(
                f"context.{number}",
                CONTEXT_EVENT_TYPE,
                TIME,
                "diagnosis_test",
                {
                    "outcome_event_id": f"effect.{number}",
                    "label_status": "verified_effect",
                    "recognition_reliability": None,
                    "decision_expected_success": 0.9,
                    "outcome_conflict": False,
                    "outcome_abstained": False,
                },
            )
            steps.append(
                ExperienceStep(
                    f"step.{number}", number, TIME, "diagnosis_test", (effect, context)
                )
            )
        episode = ExperienceEpisode("episode.one", TIME, "diagnosis_test", tuple(steps))
        report = diagnose_episodes([episode], TrainingDiagnosisPolicy())
        result = propose_updates(report, CHAMPIONS)
        self.assertEqual(result.proposals[0].failure_kind, "decision_overconfident")


class ChallengerTest(unittest.TestCase):
    def setUp(self):
        self.proposals = proposals_for(overconfident(1, 2, 3)).proposals
        self.calibration = self.proposals[0]
        self.prototype = self.proposals[1]

    def test_build_challenger_does_not_mutate_champion(self):
        before = DECISION.to_dict()
        artifact = build_challenger(self.calibration, DECISION, created_at=TIME)
        self.assertEqual(DECISION.to_dict(), before)
        self.assertIs(artifact.rollback_parameters(), DECISION)
        self.assertEqual(
            artifact.challenger.confidence_scale,
            round(1.0 - self.calibration.magnitude, 6),
        )
        self.assertNotEqual(artifact.challenger.sha256, DECISION.sha256)
        self.assertTrue(artifact.challenger.version.startswith("decision.v1+c."))
        self.assertEqual(artifact.to_dict()["status"], "challenger")

    def test_stale_champion_is_rejected(self):
        changed = replace(DECISION, acceptance_threshold=0.6)
        with self.assertRaises(ChallengerUpdateError) as context:
            build_challenger(self.calibration, changed, created_at=TIME)
        self.assertEqual(code_of(context), "stale_champion")

    def test_scope_mismatch_is_rejected(self):
        for other in (RECOGNITION, replace(DECISION, scope=OTHER_SCOPE)):
            with self.subTest(other=other):
                with self.assertRaises(ChallengerUpdateError) as context:
                    build_challenger(self.calibration, other, created_at=TIME)
                self.assertEqual(code_of(context), "scope_mismatch")

    def test_external_proposal_is_revalidated(self):
        forged = (
            replace(self.calibration, magnitude=0.2),
            replace(self.calibration, update_policy_fingerprint="0" * 16),
            replace(
                self.calibration,
                strategy="threshold",
                parameter="acceptance_threshold",
                direction="increase",
            ),
        )
        for proposal in forged:
            with self.subTest(proposal=proposal.to_dict()):
                with self.assertRaises(ChallengerUpdateError) as context:
                    build_challenger(proposal, DECISION, created_at=TIME)
                self.assertEqual(code_of(context), "invalid_proposal")
        disabled = ChallengerUpdatePolicy(enabled_strategies=("prototype",))
        with self.assertRaises(ChallengerUpdateError) as context:
            build_challenger(self.calibration, DECISION, disabled, created_at=TIME)
        self.assertEqual(code_of(context), "invalid_proposal")

    def test_invalid_created_at(self):
        for value in (
            "2026-11-05 01:00:00",
            "2026-13-01T00:00:00Z",
            1,
            "2026-11-05T01:00:00+09:00",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ChallengerUpdateError) as context:
                    build_challenger(self.calibration, DECISION, created_at=value)
                self.assertEqual(code_of(context), "invalid_created_at")

    def test_artifact_records_version_and_provenance(self):
        artifact = build_challenger(self.prototype, DECISION, created_at=TIME)
        data = artifact.to_dict()
        provenance = data["provenance"]
        self.assertEqual(provenance["builder_rule_version"], 1)
        self.assertEqual(provenance["diagnosis_id"], self.prototype.diagnosis_id)
        self.assertEqual(provenance["created_at"], TIME)
        self.assertEqual(provenance["lineage_depth"], 1)
        self.assertEqual(
            data["evaluation_contract"]["compare"],
            {
                "champion_sha256": DECISION.sha256,
                "challenger_sha256": artifact.challenger.sha256,
            },
        )
        self.assertEqual(
            data["evaluation_contract"]["trigger_refs"],
            [ref.to_dict() for ref in self.prototype.strict_refs],
        )
        later = build_challenger(
            self.prototype, DECISION, created_at="2026-11-06T00:00:00Z"
        )
        self.assertEqual(later.artifact_id, artifact.artifact_id)

    def test_lineage_depth_and_version_limit(self):
        first = build_challenger(self.calibration, DECISION, created_at=TIME)
        child_proposal = proposals_for(
            overconfident(1, 2, 3), champions=(first.challenger, RECOGNITION)
        ).proposals[0]
        child = build_challenger(child_proposal, first.challenger, created_at=TIME)
        self.assertEqual(child.lineage_depth, 2)
        deep = replace(DECISION, version="v" + "+c.abcdef012345" * 8)
        proposal = proposals_for(overconfident(1, 2), champions=(deep,)).proposals[0]
        with self.assertRaises(ChallengerUpdateError) as context:
            build_challenger(proposal, deep, created_at=TIME)
        self.assertEqual(code_of(context), "version_too_long")

    def test_resource_cost_is_deterministic(self):
        constant = build_challenger(self.calibration, DECISION, created_at=TIME)
        linear = build_challenger(self.prototype, DECISION, created_at=TIME)
        self.assertEqual(constant.resource_cost.compute_class, "constant")
        self.assertEqual(linear.resource_cost.compute_class, "linear_in_prototypes")
        self.assertEqual(linear.resource_cost.prototype_count, 3)
        self.assertGreater(linear.resource_cost.delta_bytes, 0)
        self.assertEqual(
            build_challenger(self.prototype, DECISION, created_at=TIME).resource_cost,
            linear.resource_cost,
        )


class StorageTest(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("TEMP") or None
        self.directory = tempfile.TemporaryDirectory(dir=base)
        self.store = FileSystemArtifactStore(Path(self.directory.name))
        proposal = proposals_for(overconfident(1, 2, 3)).proposals[1]
        self.artifact = build_challenger(proposal, DECISION, created_at=TIME)

    def tearDown(self):
        self.directory.cleanup()

    def test_save_load_round_trip_and_rollback_parameters(self):
        reference = save_challenger(self.store, self.artifact)
        loaded = load_challenger(self.store, reference)
        self.assertEqual(loaded, self.artifact)
        self.assertEqual(loaded.rollback_parameters(), DECISION)
        self.assertEqual(
            ChallengerArtifact.from_dict(self.artifact.to_dict()), self.artifact
        )

    def test_load_checks_version_and_optional_policy(self):
        renamed = replace(
            self.artifact,
            challenger=replace(self.artifact.challenger, version="decision.v1+c.x"),
        )
        with self.assertRaises(ChallengerUpdateError) as context:
            load_challenger(self.store, save_challenger(self.store, renamed))
        self.assertEqual(code_of(context), "invalid_artifact")
        reference = save_challenger(self.store, self.artifact)
        self.assertEqual(
            load_challenger(self.store, reference, ChallengerUpdatePolicy()),
            self.artifact,
        )
        with self.assertRaises(ChallengerUpdateError) as context:
            load_challenger(
                self.store, reference, ChallengerUpdatePolicy(intensity=0.4)
            )
        self.assertEqual(code_of(context), "invalid_artifact")

    def test_unknown_rule_or_schema_version_is_rejected(self):
        for change in (
            {"builder_rule_version": 2},
            {"schema_version": True},
            {"schema_version": 2},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ChallengerUpdateError) as context:
                    replace(self.artifact, **change)
                self.assertEqual(code_of(context), "invalid_artifact")

    def test_observation_request_is_validated(self):
        request = proposals_for(
            overconfident(1, 2, label_status="supported_effect")
        ).observation_requests[0]
        for change in (
            {"confidence": 1.5},
            {"strict_support": True},
            {"uncertain_support": -1},
            {"reason": "other"},
            {"component": "planner"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ChallengerUpdateError):
                    replace(request, **change)

    def test_tampered_artifact_is_rejected(self):
        data = self.artifact.to_dict()
        tampered = {**data, "artifact_id": "challenger." + "0" * 64}
        with self.assertRaises(ChallengerUpdateError) as context:
            ChallengerArtifact.from_dict(tampered)
        self.assertEqual(code_of(context), "invalid_artifact")
        forged = replace(
            self.artifact,
            challenger=replace(self.artifact.challenger, acceptance_threshold=0.9),
        )
        reference = save_challenger(self.store, forged)
        with self.assertRaises(ChallengerUpdateError) as context:
            load_challenger(self.store, reference)
        self.assertEqual(code_of(context), "invalid_artifact")
        payload = json.dumps(data).replace(
            '"status": "challenger"', '"status": "champion"'
        )
        other = self.store.put(
            payload.encode(),
            media_type="application/json",
            artifact_type="learning.challenger",
        )
        with self.assertRaises(ChallengerUpdateError) as context:
            load_challenger(self.store, other)
        self.assertEqual(code_of(context), "invalid_artifact")
        duplicated = b'{"a": 1, "a": 2}'
        dup = self.store.put(
            duplicated,
            media_type="application/json",
            artifact_type="learning.challenger",
        )
        with self.assertRaises(ChallengerUpdateError) as context:
            load_challenger(self.store, dup)
        self.assertEqual(code_of(context), "invalid_artifact")


if __name__ == "__main__":
    unittest.main()
