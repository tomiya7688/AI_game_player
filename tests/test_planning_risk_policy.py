from dataclasses import replace
import unittest
from unittest.mock import patch

from ai_game_player.decision_context import DecisionContextBuilder
from ai_game_player.decision_verifier import DecisionVerifier, ReliabilityStatus
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_policy_contract import (
    PlanningComputationEstimate,
    PlanningRiskPolicyConfig,
)
from ai_game_player.planning_reobservation import PlanningDecisionEpisode
from ai_game_player.planning_risk_policy import PlanningRiskPolicy
from ai_game_player.planning_result import WeightedValue
from tools.planning_policy_fixture import make_belief, make_plan, observation_model


class PlanningRiskPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.belief = make_belief()
        self.episode = PlanningDecisionEpisode(self.belief.context, "goal", self.now)
        self.policy = PlanningRiskPolicy(self.episode, clock_ns=lambda: self.now)

    def decide(self, plan=None, belief=None, **kwargs):
        belief = belief or self.belief
        plan = plan or make_plan(belief)
        args = dict(
            context=belief.context,
            goal_id="goal",
            allowed_action_ids=plan.allowed_action_ids,
            run_generation=3,
            deadline_ns=10**8,
        )
        args.update(kwargs)
        return self.policy.decide(plan, belief, **args)

    def disagreement_plan(self, belief=None):
        return make_plan(
            belief or self.belief,
            {
                "a": ((WeightedValue(1, 1),), (WeightedValue(0, 1),)),
                "b": ((WeightedValue(0, 1),), (WeightedValue(1, 1),)),
            },
        )

    def test_agreement_selects_risk_acceptable_action_and_information_early_exit(self):
        result = self.decide()
        self.assertEqual(result.outcome, PlanningOutcome.SELECT)
        self.assertEqual(result.selected_action_id, "a")
        self.assertEqual(result.disagreement, "agreement")
        self.assertEqual(result.information.evpi_upper_bound, 0)
        self.assertIn("observation_upper_bound_below_cost", result.reason_codes)
        self.assertTrue(result.global_risk_known)

    def test_disagreement_observes_only_with_positive_net_real_channel_evsi(self):
        plan = self.disagreement_plan()
        missing = self.decide(plan)
        self.assertEqual(missing.outcome, PlanningOutcome.SELECT)
        self.assertIsNone(missing.information.evsi_lower)
        no_information = self.decide(
            plan, observation_model=observation_model(self.belief, informative=False)
        )
        self.assertEqual(no_information.outcome, PlanningOutcome.SELECT)
        informed = self.decide(plan, observation_model=observation_model(self.belief))
        self.assertEqual(informed.outcome, PlanningOutcome.OBSERVE_MORE)
        self.assertEqual(informed.disagreement, "disagreement")
        self.assertEqual(informed.information.evsi_lower, 0.5)
        self.assertIsNone(informed.selected_action_id)

    def test_positive_risk_neutral_evsi_does_not_bypass_posterior_risk_policy(self):
        plan = make_plan(
            self.belief,
            {
                "a": (
                    (WeightedValue(1, 0.98), WeightedValue(-1, 0.02)),
                    (WeightedValue(0, 1),),
                ),
                "b": ((WeightedValue(0.3, 1),), (WeightedValue(0.6, 1),)),
            },
        )
        result = self.decide(plan, observation_model=observation_model(self.belief))
        self.assertAlmostEqual(result.information.evsi_lower, 0.3)
        self.assertAlmostEqual(result.observation_policy_gain_lower, 0)
        self.assertEqual(result.outcome, PlanningOutcome.SELECT)
        self.assertEqual(result.selected_action_id, "b")
        self.assertIn("risk_policy_observation_gain_unestablished", result.reason_codes)

    def test_posterior_cvar_pass_does_not_prove_contingent_mixture_is_safe(self):
        self.policy = PlanningRiskPolicy(
            self.episode,
            config=PlanningRiskPolicyConfig(min_cvar=0.5, max_catastrophe=0.02),
            clock_ns=lambda: self.now,
        )
        plan = make_plan(
            self.belief,
            {
                "a": (
                    (WeightedValue(-1, 0.01), WeightedValue(1, 0.99)),
                    (WeightedValue(-1, 1),),
                ),
                "b": ((WeightedValue(-1, 1),), (WeightedValue(0.6, 1),)),
            },
        )
        model = replace(observation_model(self.belief), withhold_value=0.0)
        result = self.decide(plan, observation_model=model)
        self.assertGreater(result.information.evsi_lower, 0.1)
        self.assertGreater(result.observation_policy_gain_lower, 0.1)
        self.assertAlmostEqual(result.observation_contingent_risk.cvar_lower, 0.44)
        self.assertFalse(result.observation_contingent_risk.acceptable)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertEqual(self.episode.usage, (0, 0))

    def test_observation_budget_is_not_reset_by_new_frames_or_belief_versions(self):
        for seq in range(1, 7):
            belief = make_belief(seq=seq)
            result = self.decide(
                self.disagreement_plan(belief),
                belief,
                observation_model=observation_model(belief),
            )
            self.assertEqual(
                result.outcome,
                PlanningOutcome.OBSERVE_MORE if seq <= 2 else PlanningOutcome.SELECT,
            )
            if seq > 2:
                self.assertIn("observation_episode_exhausted", result.reason_codes)
        self.assertEqual(self.episode.usage, (2, 0.2))

    def test_rare_chance_loss_is_not_hidden_by_positive_average_or_world_agreement(
        self,
    ):
        belief = make_belief((1.0,))
        plan = make_plan(
            belief,
            {
                "risky": ((WeightedValue(1, 0.98), WeightedValue(-1, 0.02)),),
                "safe": ((WeightedValue(0.3, 1),),),
            },
        )
        result = self.decide(plan, belief)
        self.assertEqual(result.selected_action_id, "safe")
        risky = next(x for x in result.risks if x.action_id == "risky")
        self.assertAlmostEqual(risky.expected_lower, 0.96)
        self.assertAlmostEqual(risky.catastrophic_upper, 0.02)
        self.assertFalse(risky.acceptable)

    def test_interval_leaf_quality_is_not_a_probability_and_keeps_lower_tail(self):
        belief = make_belief((1.0,))
        plan = make_plan(
            belief,
            {
                "unsupported": ((WeightedValue(0.7, 1, -1, 1),),),
                "safe": ((WeightedValue(0.2, 1),),),
            },
        )
        result = self.decide(plan, belief)
        self.assertEqual(result.selected_action_id, "safe")
        unsupported = result.risks[0]
        self.assertEqual(unsupported.cvar_lower, -1)
        self.assertEqual(unsupported.cvar_upper, 1)
        self.assertEqual(unsupported.catastrophic_upper, 1)

    def test_unknown_world_or_trace_mass_is_pessimistic_and_can_escalate(self):
        belief = make_belief((0.4, 0.4))
        result = self.decide(make_plan(belief), belief)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertTrue(all(x.cvar_lower == -1 for x in result.risks))
        escalated = self.decide(make_plan(belief), belief, can_escalate=True)
        self.assertEqual(escalated.outcome, PlanningOutcome.ESCALATE)

    def test_uncalibrated_is_unknown_and_conditional_experiment_requires_opt_in(self):
        belief = make_belief((0.9,), score=True)
        plan = make_plan(belief)
        result = self.decide(plan, belief)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertEqual(result.reason_codes, ("uncalibrated_global_risk",))
        self.policy = PlanningRiskPolicy(
            self.episode,
            config=PlanningRiskPolicyConfig(allow_single_conditional=True),
            clock_ns=lambda: self.now,
        )
        result = self.decide(plan, belief)
        self.assertEqual(result.outcome, PlanningOutcome.SELECT)
        self.assertEqual(result.probability_semantics, "conditional_hypothesis")
        self.assertFalse(result.global_risk_known)
        self.assertIn("conditional_assumption_explicit", result.reason_codes)

    def test_uncalibrated_escalation_still_honors_marginal_gain_early_exit(self):
        belief = make_belief((0.9,), score=True)
        plan = make_plan(belief)
        computation = PlanningComputationEstimate(
            belief.context, 1, 0.01, 0.1, "marginal.v1", "evaluator", "v1"
        )
        result = self.decide(plan, belief, can_escalate=True, computation=computation)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertIn("computation_early_exit", result.reason_codes)
        self.assertFalse(result.continue_computation)
        result = self.decide(
            plan,
            belief,
            can_escalate=True,
            computation=replace(computation, upper_gain=0.2),
        )
        self.assertEqual(result.outcome, PlanningOutcome.ESCALATE)
        self.assertTrue(result.continue_computation)

    def test_hard_allowed_and_veto_cannot_be_overridden_by_value(self):
        result = self.decide(hard_veto_action_ids=("a",))
        self.assertEqual(result.selected_action_id, "b")
        result = self.decide(allowed_action_ids=())
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertEqual(result.reason_codes, ("no_hard_allowed_action",))
        result = self.decide(hard_veto_action_ids=("a", "b"), can_escalate=True)
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)

    def test_existing_verifier_hard_allowed_survives_soft_planning_values(self):
        observation = ScreenObservation(
            "table", 100, 100, features={"signature": "real.frame"}
        )
        candidates = [
            ActionCandidate("a", "wait", "high value", confidence=0.99),
            ActionCandidate("b", "wait", "allowed", confidence=0.9),
        ]
        verified_context = DecisionContextBuilder().build(
            observation, candidates, [candidates[1]]
        )
        result = self.decide(allowed_action_ids=verified_context.allowed_action_ids)
        self.assertEqual(result.selected_action_id, "b")
        decision = ActionDecision(
            result.selected_action_id,
            "risk policy",
            "planning.risk",
            snapshot_id=verified_context.snapshot_id,
        )
        verification = DecisionVerifier().verify(
            decision, verified_context, observation
        )
        self.assertNotEqual(verification.status, ReliabilityStatus.REJECT)
        denied = DecisionVerifier().verify(
            replace(decision, action_id="a"), verified_context, observation
        )
        self.assertEqual(denied.status, ReliabilityStatus.REJECT)

    def test_stale_scope_version_generation_and_failed_planning_do_not_act(self):
        plan = make_plan(self.belief)
        for bad in (
            replace(plan, context=replace(plan.context, session_id="other")),
            replace(plan, belief_version=2),
            replace(plan, run_generation=4),
            replace(plan, created_at_ns=1001, compute_cutoff_at_ns=2000),
            replace(plan, expires_at_ns=1000),
            replace(plan, compute_cutoff_at_ns=199),
            replace(plan, value_unit="different.unit"),
            replace(
                plan,
                outcome=PlanningOutcome.ABSTAIN,
                selected_action_id=None,
                completed_depth=0,
                action_values=(),
            ),
        ):
            with self.subTest(bad=bad):
                result = self.decide(bad)
                self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                self.assertIsNone(result.selected_action_id)

    def test_mismatched_observation_or_computation_source_is_rejected(self):
        plan = self.disagreement_plan()
        model = observation_model(self.belief)
        for bad in (
            replace(model, belief_version=2),
            replace(model, evaluator_version="v2"),
            replace(model, value_unit="seconds"),
            replace(model, hypothesis_ids=("h1", "h0")),
        ):
            self.assertEqual(
                self.decide(plan, observation_model=bad).outcome,
                PlanningOutcome.ABSTAIN,
            )
        gain = PlanningComputationEstimate(
            self.belief.context, 1, 0.01, 0.1, "marginal.v1", "evaluator", "v1"
        )
        result = self.decide(computation=gain)
        self.assertFalse(result.continue_computation)
        self.assertIn("computation_early_exit", result.reason_codes)
        result = self.decide(computation=replace(gain, upper_gain=0.2))
        self.assertTrue(result.continue_computation)
        self.assertEqual(
            self.decide(computation=replace(gain, evaluator_id="other")).outcome,
            PlanningOutcome.ABSTAIN,
        )

    def test_timeout_before_final_publication_does_not_authorize_execution_ack(self):
        with patch.object(
            self.policy, "_now", side_effect=[1000, 1000, 1000, 1000, 1000, 1000, 10**9]
        ):
            result = self.decide()
        self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
        self.assertIsNone(result.selected_action_id)
        self.assertIsNone(self.episode._pending)

    def test_interval_disagreement_unknown_failure_clock_and_budget(self):
        plan = make_plan(
            self.belief,
            {
                "a": ((WeightedValue(0.5, 1, 0, 1),), (WeightedValue(0.5, 1, 0, 1),)),
                "b": ((WeightedValue(0.5, 1, 0, 1),), (WeightedValue(0.5, 1, 0, 1),)),
            },
        )
        result = self.decide(plan)
        self.assertEqual(result.disagreement, "unknown")
        self.policy = PlanningRiskPolicy(
            self.episode,
            config=PlanningRiskPolicyConfig(max_trace_atoms=1),
            clock_ns=lambda: self.now,
        )
        self.assertEqual(self.decide().reason_codes, ("policy_trace_budget",))
        self.policy = PlanningRiskPolicy(self.episode, clock_ns=lambda: float("nan"))
        self.assertEqual(self.decide().outcome, PlanningOutcome.ABSTAIN)

    def test_corrupt_distribution_and_wrong_hypothesis_weight_are_rejected(self):
        plan = make_plan(self.belief)
        object.__setattr__(
            plan.action_values[0].outcomes[0], "probability", float("nan")
        )
        self.assertEqual(self.decide(plan).outcome, PlanningOutcome.ABSTAIN)
        plan = make_plan(self.belief)
        h = plan.action_values[0].hypothesis_values[0]
        object.__setattr__(h, "belief_weight", 0.9)
        self.assertEqual(self.decide(plan).outcome, PlanningOutcome.ABSTAIN)
