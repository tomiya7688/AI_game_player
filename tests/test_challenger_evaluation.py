import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ai_game_player.artifact_store import FileSystemArtifactStore
from ai_game_player.challenger_evaluation import (
    evaluate_candidate,
    load_evaluation_event,
    save_evaluation_event,
)
from ai_game_player.challenger_evaluation_models import (
    EvaluationCase,
    EvaluationError,
    EvaluationPolicy,
    PredictionInput,
)
from ai_game_player.challenger_promotion import ChampionRegistry
from ai_game_player.challenger_promotion_models import PromotionCriteria
from ai_game_player.challenger_update import build_challenger
from tests.test_challenger_update import (
    DECISION,
    OTHER_SCOPE,
    TIME,
    case,
    overconfident,
    proposals_for,
)

EVALUATED = "2026-11-06T00:30:00Z"
LATER = "2026-11-06T01:00:00Z"
POLICY = EvaluationPolicy(min_scored_per_group=10)


class FakeClock:
    """呼ぶたびに一定秒進む時計。1症例の採点が呼出し2回=step秒になる。"""

    def __init__(self, step):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


def group(prefix, count, group_name, signal, outcome, **overrides):
    return [
        EvaluationCase(
            case(
                f"{prefix}{n}",
                decision_expected_success=signal,
                outcome=outcome,
                **overrides,
            ),
            group_name,
        )
        for n in range(count)
    ]


class EvaluationRunnerTest(unittest.TestCase):
    def setUp(self):
        proposal = proposals_for(overconfident(1, 2, 3)).proposals[0]
        self.artifact = build_challenger(proposal, DECISION, created_at=TIME)
        self.assertLess(self.artifact.challenger.confidence_scale, 1.0)

    def run_eval(self, cases, **kwargs):
        kwargs.setdefault("evaluation_ref", "eval.one")
        kwargs.setdefault("evaluated_at", EVALUATED)
        return evaluate_candidate(self.artifact, cases, POLICY, **kwargs)

    def improving_cases(self):
        # signal=0.5: Championは採用(0.5>=0.5)して誤り、Challengerは較正で不採用にして正解。
        # 回帰群はsignal=0.9で両者とも採用して正解のまま。
        return group("t", 12, "target", 0.5, "failure") + group(
            "r", 12, "regression", 0.9, "success"
        )

    def test_improvement_and_regression_are_aggregated_separately(self):
        evaluation, event = self.run_eval(self.improving_cases())
        self.assertEqual(
            (evaluation.target_baseline, evaluation.target_candidate), (0.0, 1.0)
        )
        self.assertEqual(
            (evaluation.regression_baseline, evaluation.regression_candidate),
            (1.0, 1.0),
        )
        self.assertEqual(
            (evaluation.target_cases, evaluation.regression_cases), (12, 12)
        )
        self.assertEqual(event.signal, "escalate")
        self.assertEqual(event.validation_state, "offline")
        self.assertEqual(event.groups["target"]["challenger"].correct, 12)

    def test_regression_lowers_the_level_signal(self):
        cases = group("t", 12, "target", 0.5, "failure") + group(
            "r", 12, "regression", 0.5, "success"
        )
        evaluation, event = self.run_eval(cases)
        self.assertEqual(evaluation.regression_candidate, 0.0)
        self.assertEqual(
            (event.signal, event.reasons), ("de_escalate", ("regression",))
        )

    def test_insufficient_evidence_holds(self):
        cases = group("t", 3, "target", 0.5, "failure") + group(
            "r", 3, "regression", 0.9, "success"
        )
        _, event = self.run_eval(cases)
        self.assertEqual(
            (event.signal, event.reasons), ("hold", ("insufficient_evidence",))
        )

    def test_cases_without_verified_labels_are_not_scored(self):
        cases = self.improving_cases() + group("u", 4, "target", 0.5, "unknown")
        cases += group(
            "h", 2, "target", 0.5, "failure", label_status="supported_effect"
        )
        evaluation, event = self.run_eval(cases)
        self.assertEqual(evaluation.target_cases, 12)
        self.assertEqual(event.groups["target"]["champion"].unscorable, 6)

    def test_errors_and_timeouts_count_as_failures_in_the_denominator(self):
        def predictor(parameters, _case):
            if parameters.sha256 == self.artifact.challenger.sha256:
                raise ValueError("boom")
            return True

        evaluation, event = self.run_eval(self.improving_cases(), predictor=predictor)
        challenger = event.groups["target"]["challenger"]
        self.assertEqual(
            (challenger.scored, challenger.failed, challenger.correct), (12, 12, 0)
        )
        self.assertEqual(evaluation.target_cases, 12)
        _, slow = self.run_eval(
            self.improving_cases(), clock=FakeClock(1.0)
        )  # 1症例=1000ms > 50ms
        self.assertEqual(slow.groups["target"]["champion"].failed, 12)
        self.assertEqual(slow.groups["target"]["champion"].accuracy, 0.0)

    def test_any_exception_type_counts_as_a_failure_but_base_exceptions_propagate(self):
        for error in (TimeoutError("late"), RuntimeError("provider"), OSError("io")):
            with self.subTest(error=type(error).__name__):

                def predictor(_parameters, _case, error=error):
                    raise error

                _, event = self.run_eval(self.improving_cases(), predictor=predictor)
                self.assertEqual(event.groups["target"]["champion"].failed, 12)
                self.assertEqual(event.groups["target"]["champion"].scored, 12)

        def interrupted(_parameters, _case):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_eval(self.improving_cases(), predictor=interrupted)

    def test_regression_cases_replaced_by_timeouts_are_never_promotable(self):
        # 回帰群は元から不正解(Champion=False)でも、Challengerが全例Timeoutへ置換したらaccuracyは同じでも劣化として扱う。
        cases = group("t", 12, "target", 0.5, "failure") + group(
            "r", 12, "regression", 0.5, "success"
        )

        def predictor(parameters, case):
            is_challenger = parameters.sha256 == self.artifact.challenger.sha256
            if case.ref.step_id.startswith("step.r"):
                if is_challenger:
                    raise TimeoutError("late")
                return False
            return not is_challenger

        evaluation, event = self.run_eval(cases, predictor=predictor)
        regression = event.groups["regression"]
        self.assertEqual(
            regression["champion"].accuracy, regression["challenger"].accuracy
        )
        self.assertEqual(regression["challenger"].failed, 12)
        self.assertEqual(
            (event.signal, event.reasons), ("de_escalate", ("regression_failures",))
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "registry.json")
            registry.initialize(DECISION, at=TIME)
            decision = registry.decide(
                self.artifact, evaluation, PromotionCriteria(), at=LATER
            )
            self.assertFalse(decision.promoted)
            self.assertIn("insufficient_regression_cases", decision.reasons)

    def test_latency_is_measured_per_arm(self):
        _, event = self.run_eval(self.improving_cases(), clock=FakeClock(0.001))
        self.assertAlmostEqual(
            event.groups["target"]["challenger"].latency_p95_ms, 1.0, places=3
        )
        self.assertEqual(event.resource_cost["compute_class"], "constant")

    def test_same_case_is_scored_by_both_arms(self):
        seen = []

        def predictor(parameters, scored_case):
            seen.append((scored_case.ref, parameters.sha256))
            return True

        self.run_eval(self.improving_cases()[:2], predictor=predictor)
        by_case = {}
        for case_id, sha in seen:
            by_case.setdefault(case_id, set()).add(sha)
        self.assertEqual({len(shas) for shas in by_case.values()}, {2})

    def test_invalid_inputs_are_rejected(self):
        wrong_scope = [
            EvaluationCase(
                replace(case(1, decision_expected_success=0.5), scope=OTHER_SCOPE),
                "target",
            )
        ]
        duplicate = group("t", 1, "target", 0.5, "failure") * 2
        shared = group("s", 1, "target", 0.5, "failure") + [
            EvaluationCase(item.case, "regression")
            for item in group("s", 1, "target", 0.5, "failure")
        ]
        for cases, code in (
            (wrong_scope, "scope_mismatch"),
            (duplicate, "duplicate_case"),
            (shared, "duplicate_case"),
            (["x"], "invalid_case"),
        ):
            with self.subTest(code=code), self.assertRaises(EvaluationError) as context:
                self.run_eval(cases)
            self.assertEqual(context.exception.code, code)
        with self.assertRaises(EvaluationError):
            EvaluationCase(case(1), "other")
        with self.assertRaises(EvaluationError):
            EvaluationPolicy(min_gain=2)

    def test_evaluation_feeds_the_promotion_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "registry.json")
            registry.initialize(DECISION, at=TIME)
            evaluation, _ = self.run_eval(self.improving_cases())
            decision = registry.decide(
                self.artifact, evaluation, PromotionCriteria(), at=LATER
            )
            self.assertTrue(decision.promoted)
            regress = group("t", 12, "target", 0.5, "failure") + group(
                "r", 12, "regression", 0.5, "success"
            )
            registry2 = ChampionRegistry(Path(directory) / "registry2.json")
            registry2.initialize(DECISION, at=TIME)
            bad, _ = self.run_eval(regress)
            rejected = registry2.decide(
                self.artifact, bad, PromotionCriteria(), at=LATER
            )
            self.assertEqual(rejected.reasons, ("regression",))


class PrototypeEvaluationTest(unittest.TestCase):
    """prototypeだけ違うChallengerを、改善0と測らず評価不能として扱う(オーナーレビューのP1)。"""

    def setUp(self):
        proposals = proposals_for(overconfident(1, 2, 3)).proposals
        self.artifact = build_challenger(proposals[1], DECISION, created_at=TIME)
        self.assertEqual(self.artifact.proposal.strategy, "prototype")
        self.assertEqual(
            self.artifact.challenger.confidence_scale,
            self.artifact.parent.confidence_scale,
        )
        self.assertEqual(
            self.artifact.challenger.acceptance_threshold,
            self.artifact.parent.acceptance_threshold,
        )
        # case(1..3)はprototypeの参照と同じref。負例と同じ症例は、prototype付きなら不採用になるべき。
        self.cases = (
            [
                EvaluationCase(
                    case(n, decision_expected_success=0.9, outcome="failure"), "target"
                )
                for n in (1, 2, 3)
            ]
            + group("t", 9, "target", 0.9, "success")
            + group("r", 12, "regression", 0.9, "success")
        )

    def evaluate(self, **kwargs):
        return evaluate_candidate(
            self.artifact,
            self.cases,
            POLICY,
            evaluation_ref="eval.proto",
            evaluated_at=EVALUATED,
            **kwargs,
        )

    def test_the_builtin_predictor_cannot_tell_a_prototype_only_challenger_apart(self):
        # 根拠: 組込みの採点は全症例でChampionと同じ予測になる(だから「改善0」と測ってはいけない)。
        from ai_game_player.challenger_evaluation import predict_accept

        for item in self.cases:
            self.assertEqual(
                predict_accept(self.artifact.parent, item.case),
                predict_accept(self.artifact.challenger, item.case),
            )

    def test_unsupported_evaluation_is_held_and_never_promotable(self):
        calls = []

        def spy(_parameters, _case):
            calls.append(1)
            return True

        evaluation, event = self.evaluate(predictor=spy)
        self.assertEqual(calls, [])
        self.assertEqual(
            (event.signal, event.reasons), ("hold", ("unsupported_evaluation",))
        )
        self.assertEqual((evaluation.target_cases, evaluation.regression_cases), (0, 0))
        target = event.groups["target"]["challenger"]
        self.assertEqual(
            (target.scored, target.correct, target.failed, target.unscorable),
            (0, 0, 0, 12),
        )
        with tempfile.TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "registry.json")
            registry.initialize(DECISION, at=TIME)
            decision = registry.decide(
                self.artifact, evaluation, PromotionCriteria(), at=LATER
            )
            self.assertFalse(decision.promoted)
            self.assertIn("insufficient_target_cases", decision.reasons)

    def test_unsupported_event_round_trips_through_storage(self):
        _, event = self.evaluate()
        with tempfile.TemporaryDirectory() as directory:
            store = FileSystemArtifactStore(Path(directory))
            reference = save_evaluation_event(store, event)
            self.assertEqual(load_evaluation_event(store, reference), event)

    def test_a_prototype_aware_predictor_measures_the_negative_reference_effect(self):
        def predictor(parameters, scored):
            # 負例prototypeと同じ症例は不採用、それ以外は信号が0.5以上なら採用。
            if scored.ref in {item.ref for item in parameters.prototypes}:
                return False
            return scored.decision_expected_success >= parameters.acceptance_threshold

        evaluation, event = self.evaluate(
            predictor=predictor,
            evaluable_strategies=("calibration", "threshold", "prototype"),
        )
        self.assertEqual(event.reasons, ("improved_without_regression",))
        champion = event.groups["target"]["champion"]
        challenger = event.groups["target"]["challenger"]
        self.assertGreater(challenger.accuracy, champion.accuracy)
        self.assertEqual((champion.correct, challenger.correct), (9, 12))
        self.assertEqual(evaluation.target_cases, 12)

    def test_a_threshold_candidate_is_still_evaluated_with_the_builtin_predictor(self):
        proposals = proposals_for(overconfident(1, 2, 3)).proposals
        artifact = build_challenger(proposals[0], DECISION, created_at=TIME)
        self.assertNotEqual(artifact.proposal.strategy, "prototype")
        evaluation, event = evaluate_candidate(
            artifact,
            group("t", 12, "target", 0.5, "failure")
            + group("r", 12, "regression", 0.9, "success"),
            POLICY,
            evaluation_ref="eval.cal",
            evaluated_at=EVALUATED,
        )
        self.assertEqual(evaluation.target_cases, 12)
        self.assertNotIn("unsupported_evaluation", event.reasons)


class PredictionInputSeparationTest(unittest.TestCase):
    """予測の入力に正解ラベルを渡さない(ラベル漏洩の防止)。"""

    def test_the_predictor_receives_signals_only_never_the_ground_truth(self):
        proposal = proposals_for(overconfident(1, 2, 3)).proposals[0]
        artifact = build_challenger(proposal, DECISION, created_at=TIME)
        seen = []

        def predictor(_parameters, scored):
            seen.append(scored)
            return True

        evaluate_candidate(
            artifact,
            group("t", 12, "target", 0.5, "failure")
            + group("r", 12, "regression", 0.9, "success"),
            POLICY,
            evaluation_ref="eval.leak",
            evaluated_at=EVALUATED,
            predictor=predictor,
        )
        self.assertTrue(seen)
        for item in seen:
            self.assertIsInstance(item, PredictionInput)
            for forbidden in (
                "outcome",
                "label_status",
                "outcome_conflict",
                "outcome_abstained",
            ):
                self.assertFalse(hasattr(item, forbidden), forbidden)


class EvaluationEventStorageTest(unittest.TestCase):
    def setUp(self):
        proposal = proposals_for(overconfident(1, 2, 3)).proposals[0]
        artifact = build_challenger(proposal, DECISION, created_at=TIME)
        cases = group("t", 12, "target", 0.5, "failure") + group(
            "r", 12, "regression", 0.9, "success"
        )
        _, self.event = evaluate_candidate(
            artifact, cases, POLICY, evaluation_ref="eval.one", evaluated_at=EVALUATED
        )
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FileSystemArtifactStore(Path(self.directory.name))

    def test_round_trip(self):
        reference = save_evaluation_event(self.store, self.event)
        self.assertEqual(load_evaluation_event(self.store, reference), self.event)

    def test_tampered_event_is_rejected(self):
        value = self.event.to_dict()
        value["signal"] = "escalate" if value["signal"] != "escalate" else "hold"
        with self.assertRaises(EvaluationError):
            type(self.event).from_dict(value)
        value = self.event.to_dict()
        value["groups"]["target"]["challenger"]["correct"] = 0
        with self.assertRaises(EvaluationError):
            type(self.event).from_dict(value)

    def test_event_is_json_stable(self):
        self.assertEqual(
            json.dumps(self.event.to_dict()), json.dumps(self.event.to_dict())
        )
        self.assertTrue(self.event.to_dict()["event_id"].startswith("evaluation."))


if __name__ == "__main__":
    unittest.main()
