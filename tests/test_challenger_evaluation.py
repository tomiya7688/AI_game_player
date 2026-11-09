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

    def test_latency_is_measured_per_arm(self):
        _, event = self.run_eval(self.improving_cases(), clock=FakeClock(0.001))
        self.assertAlmostEqual(
            event.groups["target"]["challenger"].latency_p95_ms, 1.0, places=3
        )
        self.assertEqual(event.resource_cost["compute_class"], "constant")

    def test_same_case_is_scored_by_both_arms(self):
        seen = []

        def predictor(parameters, scored_case):
            seen.append((id(scored_case), parameters.sha256))
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
