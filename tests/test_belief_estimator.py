import ast
from dataclasses import replace
from itertools import repeat
from fractions import Fraction
from pathlib import Path
import random
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_game_player.belief_candidate_store import (
    BeliefCandidateStore,
    BeliefStorePolicy,
)
from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.belief_estimator import BayesianBeliefEstimator
from ai_game_player.belief_likelihood import (
    BeliefActionRef,
    BeliefEstimatorPolicy,
    BeliefLikelihoodBatch,
)
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.game_2048_transition import ReferenceGame2048TransitionModel
from ai_game_player.transition_model import ActionTransition, ChanceOutcome
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)


def context(sequence=1, **changes):
    value = BeliefContext(
        "counter",
        "board",
        "session",
        f"snapshot-{sequence}",
        (f"frame-{sequence}",),
        sequence,
        sequence,
        0,
        "screen",
        0,
        "clock",
    )
    return replace(value, **changes)


def evidence(ctx, correlation=None):
    return BeliefEvidenceRef(
        f"evidence-{ctx.frame_sequence}",
        correlation or f"correlation-{ctx.frame_sequence}",
        BeliefEvidenceKind.OBSERVATION,
        "recognizer",
        "v1",
        ctx,
        1000,
        ctx.frame_ids[-1],
    )


def hypotheses(ctx, states=(8, 16), weights=(0.6, 0.4), calibrated=True):
    kind = (
        BeliefWeightKind.CALIBRATED_PROBABILITY
        if calibrated
        else BeliefWeightKind.RELATIVE_SCORE
    )
    return tuple(
        WorldStateHypothesis(
            f"hypothesis-{index}",
            TypedStateRef(ctx.game_id, "state-v1", type(state), state),
            ctx,
            BeliefWeight(kind, weight, "fixture-prior-v1" if calibrated else None),
            HypothesisOrigin.OBSERVATION_HYPOTHESIS,
            (evidence(ctx),),
            BeliefFreshness(1000, 1_000_000_000),
        )
        for index, (state, weight) in enumerate(zip(states, weights))
    )


class ShiftModel:
    def apply_action(self, state, action_id):
        return ActionTransition(state + 1, True, 0)

    def chance_outcomes(self, after_action):
        return (ChanceOutcome(after_action.state, 1.0),)


class BeliefEstimatorTests(unittest.TestCase):
    def setUp(self):
        self.clock = SimpleNamespace(now=1000)
        self.store = BeliefCandidateStore(
            int, clock_id="clock", clock_ns=lambda: self.clock.now
        )
        self.filter = BayesianBeliefEstimator(
            self.store, clock_ns=lambda: self.clock.now
        )

    def initialize(self, weights=(0.6, 0.4), unknown=0.0, calibrated=True):
        result = self.filter.initialize(
            context(),
            hypotheses(context(), weights=weights, calibrated=calibrated),
            unknown_mass=unknown,
        )
        self.assertIsNotNone(result.snapshot, result.reason)
        return result.snapshot

    def action(self, prior, receipt="receipt-1", status="executed"):
        return BeliefActionRef(
            "advance",
            receipt,
            prior.context.snapshot_id,
            prior.context.action_epoch,
            prior.context,
            status,
        )

    def batch(
        self,
        prior,
        values,
        *,
        sequence=2,
        calibration="likelihood-v1",
        unknown=None,
        correlation=None,
        ctx=None,
    ):
        ctx = ctx or context(sequence)
        return BeliefLikelihoodBatch(
            ctx,
            BeliefFreshness(1000, 1_000_000_000),
            (evidence(ctx, correlation),),
            correlation or f"correlation-{sequence}",
            tuple(
                (item.state_ref.digest, value)
                for item, value in zip(prior.candidates, values)
            ),
            calibration,
            unknown,
        )

    def predict(self, prior, model=None, action=None):
        return self.filter.predict(
            prior,
            action or self.action(prior),
            model or ShiftModel(),
            model_id="counter-model",
            model_version="v1",
        )

    def test_observation_only_initializes_without_transition(self):
        prior = self.initialize()
        unavailable = self.filter.predict(
            prior, self.action(prior), None, model_id="none", model_version="v1"
        )
        self.assertEqual(unavailable.reason, "transition_unavailable")
        self.assertIs(self.store.read(context()), prior)
        self.assertEqual(len(prior.candidates), 2)

    def test_joint_bayesian_likelihood_reverses_ranking(self):
        prior = self.initialize()
        result = self.filter.observe(prior, self.batch(prior, (0.1, 0.9)))
        self.assertEqual(result.reason, "bayesian_posterior")
        posterior = result.snapshot
        self.assertEqual(posterior.candidates[0].state_ref.state, 16)
        self.assertAlmostEqual(posterior.candidates[0].weight.value, 0.36 / 0.42)
        self.assertEqual(posterior.residual_unknown_mass, 0.0)
        self.assertEqual(
            posterior.candidates[0].origin, HypothesisOrigin.POSTERIOR_HYPOTHESIS
        )

    def test_joint_recognizer_and_outcome_refs_apply_one_factor(self):
        prior = self.initialize()
        batch = self.batch(prior, (0.1, 0.9))
        outcome = BeliefEvidenceRef(
            "actual-outcome-2",
            batch.correlation_id,
            BeliefEvidenceKind.OUTCOME,
            "outcome-evaluator",
            "v1",
            batch.context,
            1000,
        )
        posterior = self.filter.observe(
            prior, replace(batch, evidence_refs=(*batch.evidence_refs, outcome))
        ).snapshot
        self.assertAlmostEqual(posterior.candidates[0].weight.value, 0.36 / 0.42)
        self.assertEqual(len(posterior.candidates[0].evidence_refs), 2)

    def test_seeded_posterior_matches_exact_fraction_oracle(self):
        rng = random.Random(564)
        for case in range(50):
            with self.subTest(case=case):
                self.setUp()
                counts = [rng.randint(1, 100) for _ in range(8)]
                unknown_count = rng.randint(0, 100)
                total = sum(counts) + unknown_count
                likelihoods = [rng.randint(0, 30) for _ in counts]
                unknown_likelihood = rng.randint(0, 30)
                prior = self.filter.initialize(
                    context(),
                    hypotheses(
                        context(),
                        tuple(range(8)),
                        tuple(value / total for value in counts),
                    ),
                    unknown_mass=unknown_count / total,
                ).snapshot
                batch = self.batch(
                    prior,
                    tuple(
                        likelihoods[item.state_ref.state] for item in prior.candidates
                    ),
                    unknown=unknown_likelihood,
                )
                posterior = self.filter.observe(prior, batch).snapshot
                exact = [
                    Fraction(count, total) * likelihood
                    for count, likelihood in zip(counts, likelihoods)
                ]
                residual = Fraction(unknown_count, total) * unknown_likelihood
                denominator = sum(exact, residual)
                actual = {
                    item.state_ref.state: item.weight.value
                    for item in posterior.candidates
                }
                for state, value in enumerate(exact):
                    self.assertAlmostEqual(
                        actual.get(state, 0.0), float(value / denominator), places=12
                    )
                self.assertAlmostEqual(
                    posterior.residual_unknown_mass,
                    float(residual / denominator),
                    places=12,
                )

    def test_predict_then_observe_keeps_time_and_provenance(self):
        prior = self.initialize()
        predicted = self.predict(prior).snapshot
        self.assertEqual({x.state_ref.state for x in predicted.candidates}, {9, 17})
        self.assertTrue(predicted.reobserve_requested)
        for item in predicted.candidates:
            self.assertEqual(item.origin, HypothesisOrigin.PREDICTION)
            self.assertEqual(item.freshness, prior.candidates[0].freshness)
            self.assertEqual(item.prior_action_ref, "receipt-1")
            self.assertTrue(
                all(
                    e.kind == BeliefEvidenceKind.PREDICTION and e.frame_id is None
                    for e in item.evidence_refs
                )
            )
        posterior = self.filter.observe(
            predicted, self.batch(predicted, (0.1, 0.9))
        ).snapshot
        self.assertEqual(posterior.candidates[0].state_ref.state, 17)
        self.assertEqual(posterior.candidates[0].prior_action_ref, "receipt-1")

    def test_exclusive_paths_merge_mass_before_store_dedup(self):
        prior = self.initialize()
        model = SimpleNamespace(
            apply_action=lambda state, action_id: ActionTransition(0, True, 0),
            chance_outcomes=lambda action: (ChanceOutcome(0, 1.0),),
        )
        result = self.predict(prior, model)
        self.assertEqual(len(result.snapshot.candidates), 1)
        self.assertEqual(result.snapshot.candidates[0].weight.value, 1.0)
        self.assertEqual(result.snapshot.residual_unknown_mass, 0.0)

    def test_weighted_chance_paths_are_aggregated(self):
        prior = self.filter.initialize(
            context(), hypotheses(context(), (0, 1))
        ).snapshot
        model = SimpleNamespace(
            apply_action=lambda state, action_id: ActionTransition(state, True, 0),
            chance_outcomes=lambda action: (
                ChanceOutcome(action.state, 0.75),
                ChanceOutcome(action.state + 1, 0.25),
            ),
        )
        result = self.predict(prior, model)
        weights = {
            x.state_ref.state: x.weight.value for x in result.snapshot.candidates
        }
        self.assertAlmostEqual(weights[0], 0.45)
        self.assertAlmostEqual(weights[1], 0.45)
        self.assertAlmostEqual(weights[2], 0.10)

    def test_invalid_chance_is_unknown_not_a_silent_noop(self):
        for outcomes in (
            (),
            (ChanceOutcome(1, 0.8),),
            (ChanceOutcome(1, float("nan")),),
            (ChanceOutcome([], 1.0),),
        ):
            with self.subTest(outcomes=outcomes):
                self.setUp()
                prior = self.initialize()
                model = SimpleNamespace(
                    apply_action=lambda state, action_id: ActionTransition(
                        state, True, 0
                    ),
                    chance_outcomes=lambda action: outcomes,
                )
                result = self.predict(prior, model)
                self.assertEqual(result.snapshot.candidates, ())
                self.assertEqual(result.snapshot.residual_unknown_mass, 1.0)
                self.assertTrue(result.reobserve_requested)

    def test_capacity_truncation_preserves_unknown(self):
        self.filter = BayesianBeliefEstimator(
            self.store,
            policy=BeliefEstimatorPolicy(max_generated_states=1),
            clock_ns=lambda: self.clock.now,
        )
        prior = self.initialize()
        predicted = self.predict(prior).snapshot
        self.assertEqual(len(predicted.candidates), 1)
        self.assertAlmostEqual(predicted.residual_unknown_mass, 0.4)

    def test_expansion_payload_budget_drops_parent_mass(self):
        self.filter = BayesianBeliefEstimator(
            self.store,
            policy=BeliefEstimatorPolicy(max_expansion_bytes=1),
            clock_ns=lambda: self.clock.now,
        )
        prior = self.initialize()
        predicted = self.predict(prior).snapshot
        self.assertEqual(predicted.candidates, ())
        self.assertEqual(predicted.residual_unknown_mass, 1.0)

    def test_all_contradictions_return_unknown(self):
        prior = self.initialize()
        result = self.filter.observe(prior, self.batch(prior, (0.0, 0.0)))
        self.assertEqual(result.reason, "all_hypotheses_contradicted")
        self.assertEqual(result.snapshot.candidates, ())
        self.assertEqual(result.snapshot.residual_unknown_mass, 1.0)

    def test_unknown_world_likelihood_remains_in_denominator(self):
        prior = self.initialize(weights=(0.3, 0.3), unknown=0.4)
        posterior = self.filter.observe(
            prior, self.batch(prior, (0.5, 0.5), unknown=0.25)
        ).snapshot
        self.assertAlmostEqual(posterior.residual_unknown_mass, 0.25)
        self.assertAlmostEqual(sum(x.weight.value for x in posterior.candidates), 0.75)

    def test_unknown_or_missing_likelihood_never_claims_calibration(self):
        for weights, unknown, values in (
            ((0.3, 0.3), 0.4, (0.5, 0.5)),
            ((0.6, 0.4), 0.0, (0.5, None)),
        ):
            with self.subTest(unknown=unknown, values=values):
                self.setUp()
                prior = self.initialize(weights=weights, unknown=unknown)
                posterior = self.filter.observe(
                    prior, self.batch(prior, values)
                ).snapshot
                self.assertEqual(posterior.weight_kind, BeliefWeightKind.RELATIVE_SCORE)
                self.assertEqual(posterior.residual_unknown_mass, 1.0)
                self.assertTrue(
                    all(x.weight.calibration_id is None for x in posterior.candidates)
                )
                self.assertEqual(len(posterior.candidates), 2)

    def test_uncalibrated_prior_does_not_become_probability(self):
        prior = self.initialize(weights=(6.0, 4.0), calibrated=False)
        result = self.filter.observe(prior, self.batch(prior, (0.1, 0.9), unknown=0.5))
        self.assertEqual(result.snapshot.weight_kind, BeliefWeightKind.RELATIVE_SCORE)
        self.assertEqual(result.snapshot.residual_unknown_mass, 1.0)

    def test_log_update_avoids_multiplication_underflow(self):
        prior = self.initialize(weights=(1e-200, 1.0))
        values = tuple(
            1e-200 if item.state_ref.state == 8 else 0.0 for item in prior.candidates
        )
        result = self.filter.observe(prior, self.batch(prior, values))
        self.assertEqual(len(result.snapshot.candidates), 1)
        self.assertEqual(result.snapshot.candidates[0].state_ref.state, 8)
        self.assertEqual(result.snapshot.candidates[0].weight.value, 1.0)

    def test_same_receipt_cannot_advance_prediction_twice(self):
        prior = self.initialize()
        action = self.action(prior)
        predicted = self.predict(prior, action=action).snapshot
        duplicate = self.predict(predicted, action=action)
        self.assertEqual(duplicate.reason, "action_receipt_already_predicted")
        self.assertIsNone(duplicate.snapshot)
        self.assertIs(self.store.read(context()), predicted)

    def test_failed_transition_does_not_consume_receipt(self):
        prior = self.initialize()

        def fail(state, action_id):
            raise RuntimeError("private diagnostic must not appear")

        result = self.predict(prior, SimpleNamespace(apply_action=fail))
        self.assertEqual(result.reason, "transition_failed")
        self.assertEqual(self.predict(prior).reason, "predicted")

    def test_unknown_or_other_scope_receipt_is_not_simulated(self):
        prior = self.initialize()
        unknown = self.predict(
            prior, action=self.action(prior, status="dispatch_unknown")
        )
        self.assertEqual(unknown.reason, "action_receipt_mismatch_or_unknown")
        other = context(session_id="other-session")
        reference = BeliefActionRef("advance", "receipt", other.snapshot_id, 0, other)
        self.assertEqual(
            self.predict(prior, action=reference).reason,
            "action_receipt_mismatch_or_unknown",
        )

    def test_correlated_frame_or_static_evidence_cannot_be_counted_twice(self):
        prior = self.initialize()
        posterior = self.filter.observe(prior, self.batch(prior, (0.5, 0.5))).snapshot
        same_correlation = self.batch(
            posterior, (0.1, 0.9), sequence=3, correlation="correlation-2"
        )
        self.assertEqual(
            self.filter.observe(posterior, same_correlation).reason,
            "correlated_observation_already_used",
        )
        overlapping = self.batch(
            posterior, (0.1, 0.9), ctx=context(3, frame_ids=("frame-2", "frame-3"))
        )
        self.assertEqual(
            self.filter.observe(posterior, overlapping).reason,
            "correlated_observation_already_used",
        )

    def test_correlation_capacity_is_not_silently_evicted_by_reset(self):
        self.filter = BayesianBeliefEstimator(
            self.store,
            policy=BeliefEstimatorPolicy(max_seen_correlations=2),
            clock_ns=lambda: self.clock.now,
        )
        prior = self.initialize()
        posterior = self.filter.observe(prior, self.batch(prior, (0.5, 0.5))).snapshot
        result = self.filter.observe(
            posterior, self.batch(posterior, (0.5, 0.5), sequence=3)
        )
        self.assertEqual(result.reason, "correlation_history_capacity")
        self.filter.reset(context(3))
        self.assertEqual(
            self.filter.initialize(context(3), hypotheses(context(3))).reason,
            "correlation_history_capacity",
        )
        fresh = context(4, source_generation=1)
        self.filter.reset(fresh)
        self.assertEqual(
            self.filter.initialize(fresh, hypotheses(fresh)).reason, "initialized"
        )

    def test_old_context_epoch_and_expired_observation_are_rejected(self):
        prior = self.initialize()
        for ctx in (
            context(2, action_epoch=1),
            context(2, source_generation=1),
            context(2, scene_id="other"),
            context(2, turn=0),
        ):
            result = self.filter.observe(prior, self.batch(prior, (0.5, 0.5), ctx=ctx))
            self.assertEqual(result.reason, "observation_scope_or_order_mismatch")
        batch = replace(
            self.batch(prior, (0.5, 0.5)), freshness=BeliefFreshness(1000, 1001)
        )
        self.clock.now = 1001
        self.assertEqual(
            self.filter.observe(prior, batch).reason, "stale_or_future_observation"
        )
        self.clock.now = prior.expires_at_ns
        self.assertEqual(self.predict(prior).reason, "stale_prior")

    def test_deadline_checked_after_each_state_canonicalization(self):
        prior = self.initialize()
        model = SimpleNamespace(
            apply_action=lambda state, action_id: ActionTransition(state, True, 0),
            chance_outcomes=lambda action: tuple(
                ChanceOutcome(x, 0.25) for x in range(4)
            ),
        )

        def make_ref(*args):
            value = TypedStateRef(*args)
            self.clock.now += self.filter.policy.max_update_ns
            return value

        with patch(
            "ai_game_player.belief_prediction.TypedStateRef", side_effect=make_ref
        ) as factory:
            result = self.predict(prior, model)
        self.assertEqual(result.reason, "prediction_deadline")
        self.assertEqual(factory.call_count, 1)
        self.clock.now = 2000
        self.assertIs(self.store.read(context()), prior)

    def test_prior_bound_cas_rejects_model_side_effect_writer(self):
        prior = self.initialize()
        called = []

        def write_during_model(state, action_id):
            if not called:
                called.append(True)
                self.store.publish(
                    context(),
                    hypotheses(context()),
                    expected_version=prior.hypothesis_set_version,
                )
            return ActionTransition(state + 1, True, 0)

        result = self.predict(
            prior,
            SimpleNamespace(
                apply_action=write_during_model,
                chance_outcomes=lambda action: (ChanceOutcome(action.state, 1.0),),
            ),
        )
        self.assertEqual(result.reason, "invalid_transition_or_stale_prior")
        self.assertEqual(
            self.store.read(context()).hypothesis_set_version,
            prior.hypothesis_set_version + 1,
        )

    def test_infinite_candidate_iterable_is_bounded(self):
        candidate = hypotheses(context())[0]
        result = self.filter.initialize(context(), repeat(candidate))
        self.assertEqual(result.reason, "invalid_initial_observation")

    def test_failing_observation_iterator_preserves_previous_snapshot(self):
        prior = self.initialize()

        def failed_source():
            yield hypotheses(context(2))[0]
            raise RuntimeError("private source details")

        result = self.filter.initialize(context(2), failed_source())
        self.assertEqual(result.reason, "observation_source_failed")
        self.assertIs(self.store.read(context()), prior)

    def test_invalid_likelihoods_and_provenance_are_rejected(self):
        prior = self.initialize()
        batch = self.batch(prior, (0.1, 0.9))
        for value in (True, -0.1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(
                    batch,
                    state_likelihoods=((prior.candidates[0].state_ref.digest, value),),
                )
        with self.assertRaises(ValueError):
            replace(
                batch,
                state_likelihoods=(
                    batch.state_likelihoods[0],
                    batch.state_likelihoods[0],
                ),
            )
        with self.assertRaises(ValueError):
            replace(
                batch,
                evidence_refs=(
                    replace(
                        batch.evidence_refs[0],
                        kind=BeliefEvidenceKind.OUTCOME,
                        frame_id=None,
                    ),
                ),
            )

    def test_continuous_density_and_common_rescaling_give_same_posterior(self):
        results = []
        for factor in (1.0, 1e100, 1e-100):
            self.setUp()
            prior = self.initialize()
            posterior = self.filter.observe(
                prior, self.batch(prior, (2 * factor, 10 * factor))
            ).snapshot
            results.append(
                {
                    item.state_ref.state: item.weight.value
                    for item in posterior.candidates
                }
            )
        for result in results:
            self.assertAlmostEqual(result[16], 4.0 / 5.2)
            self.assertAlmostEqual(result[8], 1.2 / 5.2)

    def test_real_2048_fixture_predicts_ambiguous_cell_and_updates_it(self):
        ctx = context(game_id="2048")
        store = BeliefCandidateStore(
            Game2048State,
            clock_id="clock",
            clock_ns=lambda: self.clock.now,
            policy=BeliefStorePolicy(max_candidates=128, max_input_candidates=256),
        )
        estimator = BayesianBeliefEstimator(store, clock_ns=lambda: self.clock.now)
        states = tuple(
            Game2048State(((2, 2, value, 0), (0,) * 4, (0,) * 4, (0,) * 4))
            for value in (8, 16)
        )
        initial = estimator.initialize(
            ctx, hypotheses(ctx, states, (0.5, 0.5))
        ).snapshot
        action = BeliefActionRef(
            "2048-move-left", "actual-receipt", ctx.snapshot_id, 0, ctx
        )
        predicted = estimator.predict(
            initial,
            action,
            ReferenceGame2048TransitionModel(),
            model_id="2048-pinned",
            model_version="478b6ec",
        ).snapshot
        self.assertGreater(len(predicted.candidates), 2)
        self.assertEqual(predicted.residual_unknown_mass, 0.0)
        observed_ctx = context(2, game_id="2048")
        likelihoods = tuple(
            (
                item.state_ref.digest,
                0.9 if item.state_ref.state.cells[0][1] == 16 else 0.1,
            )
            for item in predicted.candidates
        )
        batch = BeliefLikelihoodBatch(
            observed_ctx,
            BeliefFreshness(1000, 1_000_000_000),
            (evidence(observed_ctx),),
            "joint-frame-2",
            likelihoods,
            "oracle-fixture-v1",
        )
        posterior = estimator.observe(predicted, batch).snapshot
        mass_16 = sum(
            item.weight.value
            for item in posterior.candidates
            if item.state_ref.state.cells[0][1] == 16
        )
        self.assertAlmostEqual(mass_16, 0.9)
        self.assertTrue(
            all(item.state_ref.state.score is None for item in posterior.candidates)
        )

    def test_estimator_modules_do_not_import_input_dom_or_screen_capture(self):
        root = Path(__file__).resolve().parents[1] / "src/ai_game_player"
        for name in (
            "belief_estimator.py",
            "belief_prediction.py",
            "belief_likelihood.py",
        ):
            tree = ast.parse((root / name).read_text())
            imports = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ]
            self.assertFalse(
                any(
                    any(
                        part in module
                        for part in (
                            "action_executor",
                            "windows_input",
                            "screen_capture",
                            "playwright",
                            "pipeline",
                        )
                    )
                    for module in imports
                ),
                name,
            )


if __name__ == "__main__":
    unittest.main()
