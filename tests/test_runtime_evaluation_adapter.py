from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.evaluation_prediction import (
    AxisPrediction,
    CandidatePrediction,
    EvaluationPredictionBatch,
    evaluation_observation_reference,
)
from ai_game_player.evaluation_primitives import PRIMITIVES
from ai_game_player.evaluation_profile import EvaluationProfile
from ai_game_player.execution_receipt import ExecutionReceipt, ExecutionObservation
from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.outcome_models import OutcomeEvent, OutcomeEvidence
from ai_game_player.runtime_evaluation_adapter import RuntimeEvaluationAdapter
from ai_game_player.runtime_evaluation_projection import RuntimeEvaluationProjection
from ai_game_player.runtime.session_composition import (
    RuntimeComposition,
    SessionRuntimeConfiguration,
)
from ai_game_player.runtime_mode_config import RuntimeMode, RuntimeModeConfig
from ai_game_player.run_control import RunController
from ai_game_player.action_executor import ExecutionResult
from ai_game_player.knowledge_evaluation import (
    KnowledgeEvaluationCase,
    KnowledgeEvaluationRunner,
)
from ai_game_player.knowledge_builder import KnowledgeBuilder


SCOPE = KnowledgeScope("generic-game", "scene", "profile", "local", "isolated")
OBSERVATION = ScreenObservation("scene", 100, 80, features={"signature": "frame-a"})
CANDIDATES = [
    ActionCandidate("left", "click", "Left", x=10, y=10, confidence=0.95),
    ActionCandidate("right", "click", "Right", x=40, y=10, confidence=0.95),
]


def context(observation=OBSERVATION):
    return DecisionContextBuilder().build(observation, CANDIDATES, CANDIDATES)


def profile(axis="progress", version="1", **kwargs):
    return EvaluationProfile(
        version=version,
        scope=SCOPE,
        goal_id="goal",
        weights=tuple((name, float(name == axis)) for name in PRIMITIVES),
        **kwargs,
    )


def predictions(observation=OBSERVATION):
    return EvaluationPredictionBatch(
        "prediction.1",
        evaluation_observation_reference(observation),
        SCOPE,
        "goal",
        (
            CandidatePrediction(
                "left",
                (
                    AxisPrediction("progress", 0.8, 1, "fixture.predictor", "1"),
                    AxisPrediction("resources", -0.7, 1, "fixture.predictor", "1"),
                ),
            ),
            CandidatePrediction(
                "right",
                (
                    AxisPrediction("progress", -0.8, 1, "fixture.predictor", "1"),
                    AxisPrediction("resources", 0.7, 1, "fixture.predictor", "1"),
                ),
            ),
        ),
    )


def execution(
    before_context, evidence, *, status="changed", generation=0, executed=True
):
    receipt = ExecutionReceipt(
        before_context.snapshot_id,
        "left",
        generation,
        "scene",
        "frame-a",
        "executed" if executed else "dry_run",
        executed,
    )
    outcome = OutcomeEvent(
        "left", status, 0.99, False, False, tuple(evidence), "fixture outcome"
    )
    return ExecutionObservation(receipt, outcome, "scene", "frame-b")


class RuntimeEvaluationAdapterTests(unittest.TestCase):
    def test_actual_uses_original_profile_while_new_candidates_use_current_profile(
        self,
    ):
        adapter = RuntimeEvaluationAdapter(
            profile("progress", "1"),
            scope=SCOPE,
            goal_id="goal",
            predictions=predictions(),
        )
        before = context()
        adapter.apply(before, OBSERVATION)
        evidence = OutcomeEvidence(
            "state_delta",
            "state_delta",
            "changed",
            0.9,
            0.95,
            "state_delta/v1",
            {
                "state_evidence_available": True,
                "changed_fields": {
                    "progress": {"before": 0, "after": 0.5},
                    "resources": {"before": 1, "after": 0.5},
                },
            },
        )
        observed = execution(before, [evidence])
        adapter.select_profile(profile("resources", "2"))
        direct = adapter.actual_evaluation(observed, 0)
        current = adapter.apply(context(), OBSERVATION, observed, 0)
        value = current.evaluation_projection.to_dict()
        self.assertEqual(value["previous_actual"]["profile"]["version"], "1")
        self.assertEqual(
            value["previous_actual"]["quality"], direct["evaluation"]["quality"]
        )
        self.assertGreater(value["previous_actual"]["quality"]["value"], 0)
        self.assertEqual(value["profile"]["version"], "2")
        self.assertEqual(
            BaselineDecisionProvider().decide(current).selected_action_id, "right"
        )
        self.assertEqual(
            current.evaluation_projection.quality_for(
                "right", current.snapshot_id
            ).profile_version,
            "2",
        )

    def test_huge_numeric_inputs_raise_value_error_instead_of_float_overflow(self):
        huge = 10**10000
        for factory in (
            lambda: EvaluationProfile(
                weights=tuple((name, huge) for name in PRIMITIVES)
            ),
            lambda: AxisPrediction("progress", huge, 1, "source", "1"),
            lambda: AxisPrediction("progress", 0, huge, "source", "1"),
        ):
            with self.assertRaises(ValueError):
                factory()

    def test_high_prediction_cannot_add_disallowed_candidate_to_real_pipeline(self):
        outside = ActionCandidate(
            "outside", "click", "Outside", x=200, y=200, confidence=1
        )
        batch = replace(
            predictions(),
            candidates=(
                *predictions().candidates,
                CandidatePrediction(
                    "outside",
                    (AxisPrediction("progress", 1, 1, "fixture.predictor", "1"),),
                ),
            ),
        )

        class Source:
            def read(self):
                return OBSERVATION, [*CANDIDATES, outside]

        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeComposition().create_session_runtime(
                SessionRuntimeConfiguration(
                    "ローカル規則",
                    "",
                    "",
                    Path(directory),
                    True,
                    None,
                    "mouse",
                    None,
                    knowledge_scope=SCOPE,
                    evaluation_profile=profile(),
                    evaluation_goal_id="goal",
                    evaluation_predictions=batch,
                ),
                source=Source(),
                controller=RunController(),
            )
            try:
                result = runtime.run_and_execute()
                self.assertEqual(result.action_id, "left")
                current = runtime.pipeline.engine.last_decision_context
                self.assertNotIn("outside", current.allowed_action_ids)
                self.assertFalse(
                    next(
                        item
                        for item in current.candidates
                        if item.action_id == "outside"
                    ).allowed
                )
                self.assertNotIn(
                    "outside",
                    BaselineDecisionProvider().decide(current).score_by_action,
                )
            finally:
                runtime.close()

    def test_offline_suite_digest_includes_the_numeric_projection_used_by_provider(
        self,
    ):
        from datetime import datetime, timezone

        base = context()
        snapshots = [
            RuntimeEvaluationAdapter(
                profile(axis), scope=SCOPE, goal_id="goal", predictions=predictions()
            ).apply(base, OBSERVATION)
            for axis in ("progress", "resources")
        ]
        static = KnowledgeBuilder.empty_snapshot(
            SCOPE, "static.1", datetime.now(timezone.utc).isoformat()
        )
        reports = []
        for current in snapshots:
            case = KnowledgeEvaluationCase(
                "case.one",
                SCOPE,
                "replay",
                current,
                (("left", 1.0), ("right", -1.0)),
                "a" * 64,
            )
            report = KnowledgeEvaluationRunner().run(
                static,
                static,
                (case,),
                lambda _: BaselineDecisionProvider(min_margin=0),
            )
            reports.append(report)
        self.assertNotEqual(reports[0].suite_sha256, reports[1].suite_sha256)

    def test_real_executed_session_observation_becomes_only_previous_actual_evaluation(
        self,
    ):
        class Source:
            observation = ScreenObservation(
                "scene",
                100,
                80,
                features={"signature": "a", "state": {"progress": 0, "resources": 1}},
            )

            def read(self):
                return self.observation, CANDIDATES

        class Backend:
            has_held_inputs = False

            def execute(self, candidate):
                return ExecutionResult(
                    candidate.action_id, True, "fixture", "non-OS input"
                )

        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            control = RunController()
            runtime = RuntimeComposition().create_session_runtime(
                SessionRuntimeConfiguration(
                    "ローカル規則",
                    "",
                    "",
                    Path(directory),
                    True,
                    None,
                    "mouse",
                    None,
                    knowledge_scope=SCOPE,
                    runtime_mode=RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY),
                ),
                source=source,
                controller=control,
            )
            runtime.pipeline.executor.dry_run = False
            runtime.pipeline.executor.live_executor = Backend()
            try:
                result = runtime.run_and_execute()
                original = runtime.pipeline.engine.last_decision_context
                source.observation = ScreenObservation(
                    "scene",
                    100,
                    80,
                    features={
                        "signature": "b",
                        "state": {"progress": 0.5, "resources": 0.8},
                    },
                )
                observed = runtime.complete_execution_observation(
                    source.observation, expected_rearm_token=control.rearm_token
                )
                self.assertTrue(result.executed)
                direct = runtime.pipeline.engine.evaluation_adapter.actual_evaluation(
                    observed, control.rearm_token
                )
                self.assertEqual(direct["evaluation"]["axes"]["progress"]["value"], 0.5)
                runtime.run()
                current = runtime.pipeline.engine.last_decision_context
                value = current.evaluation_projection.to_dict()
                self.assertEqual(
                    value["previous_actual"]["reference"]["snapshot_id"],
                    original.snapshot_id,
                )
                self.assertEqual(
                    value["previous_actual"]["axes"]["progress"]["value"], 0.5
                )
                self.assertFalse(
                    any(
                        q.available
                        for q in current.evaluation_projection.candidate_qualities(
                            current.snapshot_id
                        ).values()
                    )
                )
                self.assertEqual(runtime.adaptation.runner.metrics.updates, 0)
            finally:
                runtime.close()

    def test_default_session_receives_all_axes_without_inventing_survival_or_progress(
        self,
    ):
        class Source:
            def read(self):
                return OBSERVATION, CANDIDATES

        with tempfile.TemporaryDirectory() as directory:
            runtime = RuntimeComposition().create_session_runtime(
                SessionRuntimeConfiguration(
                    "ローカル規則",
                    "",
                    "",
                    Path(directory),
                    True,
                    None,
                    "mouse",
                    None,
                    runtime_mode=RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY),
                ),
                source=Source(),
                controller=RunController(),
            )
            try:
                runtime.run()
                value = runtime.pipeline.engine.last_decision_context.evaluation_projection.to_dict()
                self.assertEqual(set(value["previous_actual"]["axes"]), set(PRIMITIVES))
                self.assertTrue(
                    all(
                        not axis["available"]
                        and axis["confidence"] == axis["value"] == 0
                        for axis in value["previous_actual"]["axes"].values()
                    )
                )
                self.assertTrue(
                    all(
                        not row["quality"]["available"]
                        for row in value["candidate_hypothetical"].values()
                    )
                )
                self.assertEqual(runtime.adaptation.runner.metrics.updates, 0)
                trace = runtime.pipeline.engine.trace.recent(1)[0]
                self.assertEqual(
                    trace["context"]["evaluation_projection"]["context_snapshot_id"],
                    runtime.pipeline.engine.last_decision_context.snapshot_id,
                )
            finally:
                runtime.close()

    def test_explicit_profile_changes_real_session_ranking_and_keeps_allowed_and_safety(
        self,
    ):
        class Source:
            def read(self):
                return OBSERVATION, CANDIDATES

        selected = []
        for axis in ("progress", "resources"):
            with self.subTest(axis=axis), tempfile.TemporaryDirectory() as directory:
                config = SessionRuntimeConfiguration(
                    "ローカル規則",
                    "",
                    "",
                    Path(directory),
                    True,
                    None,
                    "mouse",
                    None,
                    runtime_mode=RuntimeModeConfig(RuntimeMode.INFERENCE_ONLY),
                    knowledge_scope=SCOPE,
                    evaluation_profile=profile(axis),
                    evaluation_goal_id="goal",
                    evaluation_predictions=predictions(),
                )
                runtime = RuntimeComposition().create_session_runtime(
                    config, source=Source(), controller=RunController()
                )
                try:
                    result = runtime.run_and_execute()
                    selected.append(result.action_id)
                    self.assertFalse(result.executed)
                    current = runtime.pipeline.engine.last_decision_context
                    self.assertEqual(current.allowed_action_ids, ("left", "right"))
                    self.assertEqual(
                        tuple(c.allowed for c in current.candidates), (True, True)
                    )
                    self.assertEqual(
                        {e.evaluator for e in current.candidates[0].evaluations},
                        {"safety", "repetition"},
                    )
                    baseline = BaselineDecisionProvider().decide(current)
                    self.assertEqual(baseline.selected_action_id, result.action_id)
                    self.assertIn(
                        "primitive_evaluation", baseline.contributions[result.action_id]
                    )
                    self.assertIn(
                        "candidate_hypothetical_only", str(baseline.provenance)
                    )
                    trace = runtime.pipeline.engine.trace.recent(1)[0]["context"][
                        "evaluation_projection"
                    ]
                    self.assertEqual(trace["profile"]["weights"][axis], 1)
                    self.assertEqual(
                        trace["candidate_hypothetical"][result.action_id]["reference"][
                            "prediction_version"
                        ],
                        "prediction.1",
                    )
                finally:
                    runtime.close()
        self.assertEqual(selected, ["left", "right"])

    def test_actual_terminal_is_receipt_bound_and_never_becomes_next_candidate_prediction(
        self,
    ):
        adapter = RuntimeEvaluationAdapter(scope=SCOPE)
        before = context()
        adapter.apply(before, OBSERVATION)
        evidence = OutcomeEvidence(
            "terminal_text", "terminal", "success", 0.9, 0.95, "terminal_text/v1"
        )
        actual = execution(before, [evidence], status="success")
        after = adapter.apply(context(), OBSERVATION, actual, 0)
        value = after.evaluation_projection.to_dict()
        self.assertTrue(value["previous_actual"]["axes"]["progress"]["available"])
        self.assertEqual(value["previous_actual"]["axes"]["progress"]["value"], 1)
        self.assertEqual(
            value["previous_actual"]["axes"]["progress"]["source_id"],
            "observed_terminal_pattern",
        )
        self.assertTrue(
            all(
                not q.available
                for q in after.evaluation_projection.candidate_qualities(
                    after.snapshot_id
                ).values()
            )
        )
        self.assertNotIn(
            "primitive_evaluation",
            BaselineDecisionProvider(min_margin=0).decide(after).contributions["left"],
        )

    def test_model_success_claim_and_pixel_change_do_not_become_actual_semantic_axes(
        self,
    ):
        for evidence in (
            (
                OutcomeEvidence(
                    "semantic", "terminal", "success", 1, 1, "model.claim/v1"
                ),
            ),
            (
                OutcomeEvidence(
                    "screen_diff", "screen_change", "changed", 1, 0.9, "screen_diff/v1"
                ),
            ),
        ):
            adapter = RuntimeEvaluationAdapter(scope=SCOPE)
            before = context()
            adapter.apply(before, OBSERVATION)
            actual = execution(before, evidence, status="success")
            value = adapter.apply(
                context(), OBSERVATION, actual, 0
            ).evaluation_projection.to_dict()
            self.assertTrue(
                all(
                    not value["previous_actual"]["axes"][name]["available"]
                    for name in (
                        "progress",
                        "survival",
                        "resources",
                        "irreversible_loss",
                    )
                )
            )
            self.assertFalse(value["previous_actual"]["quality"]["available"])

    def test_actual_numeric_state_and_perceptual_cues_keep_missing_axes(self):
        adapter = RuntimeEvaluationAdapter(scope=SCOPE)
        before = context()
        adapter.apply(before, OBSERVATION)
        evidence = OutcomeEvidence(
            "state_delta",
            "state_delta",
            "changed",
            0.9,
            0.95,
            "state_delta/v1",
            {
                "state_evidence_available": True,
                "changed_fields": {
                    "progress": {"before": 0.1, "after": 0.6},
                    "resources": {"before": 1, "after": 0.8},
                    "score": {"before": 0, "after": 100},
                },
            },
        )
        projected = adapter.apply(
            context(), OBSERVATION, execution(before, [evidence]), 0
        ).evaluation_projection.to_dict()
        axes = projected["previous_actual"]["axes"]
        self.assertAlmostEqual(axes["progress"]["value"], 0.5)
        self.assertAlmostEqual(axes["resources"]["value"], -0.2)
        self.assertFalse(axes["survival"]["available"])
        self.assertFalse(axes["irreversible_loss"]["available"])
        self.assertTrue(axes["novelty"]["available"])
        self.assertLess(projected["previous_actual"]["quality"]["coverage"], 1)

    def test_generation_scope_missing_receipt_and_dry_run_are_not_actual_measurements(
        self,
    ):
        for generation, executed in ((1, True), (0, False)):
            adapter = RuntimeEvaluationAdapter(scope=SCOPE)
            before = context()
            adapter.apply(before, OBSERVATION)
            evidence = OutcomeEvidence(
                "terminal_text", "terminal", "success", 1, 1, "terminal_text/v1"
            )
            value = adapter.apply(
                context(),
                OBSERVATION,
                execution(
                    before,
                    [evidence],
                    status="success",
                    generation=generation,
                    executed=executed,
                ),
                0,
            ).evaluation_projection.to_dict()
            self.assertFalse(value["previous_actual"]["quality"]["available"])

    def test_explicit_scene_change_discards_prior_actual_and_prior_frame_hypotheses(
        self,
    ):
        adapter = RuntimeEvaluationAdapter(
            profile(), scope=SCOPE, goal_id="goal", predictions=predictions()
        )
        before = context()
        adapter.apply(before, OBSERVATION)
        evidence = OutcomeEvidence(
            "terminal_text", "terminal", "success", 1, 1, "terminal_text/v1"
        )
        old_actual = execution(before, [evidence], status="success")
        changed = replace(SCOPE, scene_id="other")
        adapter.select_context(changed, "other-goal")
        current = adapter.apply(context(), OBSERVATION, old_actual, 0)
        value = current.evaluation_projection.to_dict()
        self.assertFalse(value["previous_actual"]["quality"]["available"])
        self.assertFalse(
            any(
                q.available
                for q in current.evaluation_projection.candidate_qualities(
                    current.snapshot_id
                ).values()
            )
        )
        self.assertEqual(value["scope"]["scene_id"], "other")

    def test_partial_prediction_coverage_is_not_renormalized_to_certainty(self):
        batch = replace(
            predictions(),
            candidates=(
                CandidatePrediction(
                    "left", (AxisPrediction("progress", 0.8, 1, "profile", "1"),)
                ),
            ),
        )
        adapter = RuntimeEvaluationAdapter(
            scope=SCOPE, goal_id="goal", predictions=batch
        )
        current = context()
        projected = adapter.apply(current, OBSERVATION)
        quality = projected.evaluation_projection.quality_for(
            "left", current.snapshot_id
        )
        self.assertLess(quality.coverage, 1)
        self.assertEqual(quality.confidence, quality.coverage)
        self.assertIn("survival", quality.missing_axes)
        self.assertLess(quality.bounds[0], 0)

    def test_stale_frame_scope_and_profile_mismatch_fall_back_without_candidate_mutation(
        self,
    ):
        for batch in (
            replace(predictions(), observation_ref="frame.stale"),
            replace(predictions(), scope=replace(SCOPE, scene_id="other")),
        ):
            original = context()
            projected = RuntimeEvaluationAdapter(
                profile(), scope=SCOPE, goal_id="goal", predictions=batch
            ).apply(original, OBSERVATION)
            self.assertEqual(projected.candidates, original.candidates)
            self.assertFalse(
                any(
                    q.available
                    for q in projected.evaluation_projection.candidate_qualities(
                        original.snapshot_id
                    ).values()
                )
            )
        mismatch = RuntimeEvaluationAdapter(
            profile(),
            scope=replace(SCOPE, game_id="other"),
            goal_id="goal",
            predictions=predictions(),
        )
        projected = mismatch.apply(context(), OBSERVATION)
        self.assertEqual(
            projected.evaluation_projection.to_dict()["status"], "fallback"
        )

    def test_step_profile_version_is_immutable_and_model_summary_is_opt_in(self):
        adapter = RuntimeEvaluationAdapter(
            profile(), scope=SCOPE, goal_id="goal", predictions=predictions()
        )
        current = context()
        first = adapter.apply(current, OBSERVATION)
        before = first.evaluation_projection.to_dict()
        adapter.select_profile(profile("resources", "2"))
        second = adapter.apply(context(), OBSERVATION)
        self.assertEqual(first.evaluation_projection.to_dict(), before)
        self.assertEqual(
            second.evaluation_projection.to_dict()["profile"]["version"], "2"
        )
        self.assertNotIn("evaluation_projection", first.to_dict())
        self.assertIn("evaluation_projection", first.to_dict(include_evaluation=True))
        self.assertFalse(
            first.evaluation_projection.quality_for("left", "different-step").available
        )
        exposed = RuntimeEvaluationAdapter(
            profile(),
            scope=SCOPE,
            goal_id="goal",
            predictions=predictions(),
            expose_to_models=True,
        ).apply(context(), OBSERVATION)
        self.assertNotIn("scope", exposed.to_dict()["evaluation_projection"])

    def test_zero_weight_policy_and_mutated_projection_do_not_invent_available_quality(
        self,
    ):
        zero = replace(profile(), weights=tuple((name, 0.0) for name in PRIMITIVES))
        current = context()
        projected = RuntimeEvaluationAdapter(
            zero, scope=SCOPE, goal_id="goal", predictions=predictions()
        ).apply(current, OBSERVATION)
        self.assertFalse(
            projected.evaluation_projection.quality_for(
                "left", current.snapshot_id
            ).available
        )
        altered = projected.evaluation_projection.to_dict()
        altered["candidate_hypothetical"]["left"]["axes"]["survival"]["confidence"] = 1
        with self.assertRaises(ValueError):
            RuntimeEvaluationProjection.from_dict(altered)
        altered = projected.evaluation_projection.to_dict()
        altered["candidate_hypothetical"]["left"]["quality"].update(
            value=1, confidence=1, available=True, coverage=1
        )
        with self.assertRaises(ValueError):
            RuntimeEvaluationProjection.from_dict(altered)


if __name__ == "__main__":
    unittest.main()
