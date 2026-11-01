from dataclasses import replace
import unittest

from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.expectimax_search import ExpectimaxSearch, SearchHypothesis
from ai_game_player.planning_contract import (
    PlanningDomain,
    PlanningRegistration,
    SearchBudget,
)
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_policy_contract import PlanningRiskPolicyConfig
from ai_game_player.planning_reobservation import PlanningDecisionEpisode
from ai_game_player.planning_risk_policy import PlanningRiskPolicy
from ai_game_player.transition_model import ActionTransition, ChanceOutcome
from tools.planning_policy_fixture import CardWorld, make_belief, make_plan


class PureChanceModel:
    def apply_action(self, state, action_id):
        return ActionTransition(
            CardWorld(10 if action_id == "risky" else 20), True, 0.0
        )

    def chance_outcomes(self, after):
        if after.state.hidden_card == 10:
            return (
                ChanceOutcome(CardWorld(100), 0.98),
                ChanceOutcome(CardWorld(-100), 0.02),
            )
        return (ChanceOutcome(CardWorld(30), 1.0),)


class FixedQuality:
    def evaluate(self, state):
        return NumericQuality(
            state.hidden_card / 100, 1.0, True, profile_version="quality.profile.v1"
        )


class PlanningPolicySearchIntegrationTests(unittest.TestCase):
    def plan(self, belief, *, trace_values=32):
        domain = PlanningDomain(
            CardWorld,
            lambda _: CardWorld(0),
            PureChanceModel(),
            lambda _: ("risky", "safe"),
            FixedQuality(),
        )
        registration = PlanningRegistration(
            "cards.fixture",
            "tools.planning_policy_fixture",
            "unused",
            "cards",
            "card.v1",
            "model",
            "v1",
            "evaluator",
            "v1",
            quality_profile_version="quality.profile.v1",
        )
        search = ExpectimaxSearch(
            domain,
            registration,
            SearchBudget(max_depth=1, max_trace_values=trace_values),
            tuple(
                SearchHypothesis(
                    candidate.hypothesis_id, candidate.state_ref, candidate.weight.value
                )
                for candidate in belief.candidates
            ),
            ("risky", "safe"),
            belief.weight_kind,
            belief.residual_unknown_mass,
        )
        depth, values, counters = next(search.deepen())
        return replace(
            make_plan(belief),
            selected_action_id="risky",
            allowed_action_ids=("risky", "safe"),
            action_values=values,
            counters=counters,
            completed_depth=depth,
        )

    def decide(self, plan, belief, *, conditional=False):
        episode = PlanningDecisionEpisode(belief.context, "goal", 1000)
        policy = PlanningRiskPolicy(
            episode,
            config=PlanningRiskPolicyConfig(allow_single_conditional=conditional),
            clock_ns=lambda: 1000,
        )
        return policy.decide(
            plan,
            belief,
            context=belief.context,
            goal_id="goal",
            allowed_action_ids=("risky", "safe"),
            run_generation=3,
            deadline_ns=10**8,
        )

    def test_expectimax_chance_distribution_reaches_policy_without_mean_collapse(self):
        belief = make_belief()
        plan = self.plan(belief)
        risky = next(
            action for action in plan.action_values if action.action_id == "risky"
        )
        self.assertAlmostEqual(risky.expected_quality, 0.96)
        self.assertTrue(
            any(leaf.value == -1 and leaf.probability > 0 for leaf in risky.outcomes)
        )
        decision = self.decide(plan, belief)
        self.assertEqual(decision.outcome, PlanningOutcome.SELECT)
        self.assertEqual(decision.selected_action_id, "safe")
        self.assertAlmostEqual(
            next(
                risk for risk in decision.risks if risk.action_id == "risky"
            ).catastrophic_upper,
            0.02,
        )
        self.assertGreater(plan.counters.chance_branches, 0)

    def test_trace_compression_preserves_chance_tail_uncertainty_conservatively(self):
        belief = make_belief()
        plan = self.plan(belief, trace_values=1)
        risky = next(
            action for action in plan.action_values if action.action_id == "risky"
        )
        self.assertTrue(
            risky.trace_unknown_mass > 0
            or any(leaf.lower_bound < leaf.upper_bound for leaf in risky.outcomes)
        )
        self.assertAlmostEqual(risky.expected_quality, 0.96)
        decision = self.decide(plan, belief)
        self.assertEqual(decision.selected_action_id, "safe")
        self.assertGreaterEqual(
            next(
                risk for risk in decision.risks if risk.action_id == "risky"
            ).catastrophic_upper,
            0.02 - 1e-12,
        )

    def test_relative_world_ranking_never_becomes_global_probability(self):
        belief = make_belief((0.9,), score=True)
        plan = self.plan(belief)
        self.assertTrue(
            all(
                not action.outcomes
                and action.expected_quality is None
                and action.trace_unknown_mass == 1
                for action in plan.action_values
            )
        )
        self.assertEqual(self.decide(plan, belief).outcome, PlanningOutcome.ABSTAIN)
        decision = self.decide(plan, belief, conditional=True)
        self.assertEqual(decision.selected_action_id, "safe")
        self.assertFalse(decision.global_risk_known)
        self.assertEqual(decision.probability_semantics, "conditional_hypothesis")
