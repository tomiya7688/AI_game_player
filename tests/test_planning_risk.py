from dataclasses import replace
from fractions import Fraction
import random
import unittest

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.belief_likelihood import BeliefActionRef
from ai_game_player.planning_information import (
    PlanningObservationModel,
    information_value,
)
from ai_game_player.planning_reobservation import (
    PlanningDecisionEpisode,
    PlanningObservationBudget,
)
from ai_game_player.planning_risk import lower_tail_risk


def context(sequence=1, **kwargs):
    return replace(
        BeliefContext(
            "cards",
            "table",
            "session",
            "snapshot1",
            ("frame1",),
            1,
            1,
            3,
            "capture",
            1,
            "clock",
        ),
        snapshot_id=f"snapshot{sequence}",
        frame_ids=(f"frame{sequence}",),
        frame_sequence=sequence,
        **kwargs,
    )


class PlanningRiskTests(unittest.TestCase):
    def test_chance_catastrophe_is_visible_even_with_positive_expected_value(self):
        risk = lower_tail_risk(((1.0, 0.98), (-1.0, 0.02)), 0, alpha=0.05)
        self.assertAlmostEqual(risk.expected_lower, 0.96)
        self.assertAlmostEqual(risk.lower_cvar, 0.2)
        self.assertAlmostEqual(risk.catastrophic_probability_upper, 0.02)
        self.assertEqual(risk.worst_value, -1)
        self.assertEqual(risk.lower_quantile, 1)

    def test_cvar_of_mixture_can_be_worse_than_each_conditional_cvar(self):
        first = lower_tail_risk(((-1, 0.01), (1, 0.99)), 0)
        second = lower_tail_risk(((0.6, 1),), 0)
        mixture = lower_tail_risk(((-1, 0.005), (1, 0.495), (0.6, 0.5)), 0)
        self.assertAlmostEqual(first.lower_cvar, 0.6)
        self.assertAlmostEqual(second.lower_cvar, 0.6)
        self.assertAlmostEqual(mixture.lower_cvar, 0.44)

    def test_unknown_is_lower_tail_without_renormalizing_retained_atoms(self):
        risk = lower_tail_risk(((0.7, 0.9),), 0.1, alpha=0.05)
        self.assertAlmostEqual(risk.expected_lower, 0.53)
        self.assertAlmostEqual(risk.expected_upper, 0.73)
        self.assertEqual(risk.lower_cvar, -1)
        self.assertEqual(risk.lower_quantile, -1)
        self.assertAlmostEqual(risk.catastrophic_probability_upper, 0.1)

    def test_fractional_boundary_cvar_matches_independent_fraction_oracle(self):
        rng = random.Random(566)
        for _ in range(100):
            numerators = [rng.randrange(1, 20) for _ in range(5)]
            total = sum(numerators)
            values = [Fraction(rng.randrange(-10, 11), 10) for _ in range(5)]
            masses = [Fraction(n, total) for n in numerators]
            alpha = Fraction(1, 5)
            left = alpha
            integral = Fraction(0)
            for value, mass in sorted(zip(values, masses)):
                integral += value * min(left, mass)
                left = max(Fraction(0), left - mass)
            actual = lower_tail_risk(
                tuple((float(v), float(p)) for v, p in zip(values, masses)),
                0,
                alpha=float(alpha),
            )
            self.assertAlmostEqual(actual.lower_cvar, float(integral / alpha))

    def test_invalid_distributions_are_rejected(self):
        for atoms, unknown in [
            (((0, 0.2),), 0),
            (((0, 1.1),), 0),
            (((float("nan"), 1),), 0),
            (((True, 1),), 0),
            (((0, -1),), 1),
            (((0, 1),), 0.1),
            ([(0, 1)], 0),
        ]:
            with self.subTest(atoms=atoms), self.assertRaises(ValueError):
                lower_tail_risk(atoms, unknown)
        for alpha in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                lower_tail_risk(((0, 1),), 0, alpha=alpha)

    def test_evpi_is_upper_bound_not_expected_sample_information(self):
        bounds = (((1.0, 1.0), (0.0, 0.0)), ((0.0, 0.0), (1.0, 1.0)))
        absent = information_value((0.5, 0.5), bounds, 0)
        self.assertEqual(absent.evpi_upper_bound, 0.5)
        self.assertIsNone(absent.evsi_lower)
        uninformative = information_value(
            (0.5, 0.5), bounds, 0, ((0.5, 0.5), (0.5, 0.5))
        )
        self.assertEqual(uninformative.evsi_lower, 0)
        self.assertEqual(uninformative.evsi_upper, 0)
        informative = information_value((0.5, 0.5), bounds, 0, ((1.0, 0.0), (0.0, 1.0)))
        self.assertEqual(informative.evsi_lower, 0.5)
        self.assertEqual(informative.evsi_upper, 0.5)
        self.assertTrue(informative.exact)

    def test_agreement_zero_information_and_interval_unknown_bounds(self):
        agreement = (((0.8, 0.8), (0.3, 0.3)), ((0.6, 0.6), (0.2, 0.2)))
        self.assertAlmostEqual(
            information_value((0.5, 0.5), agreement, 0).evpi_upper_bound, 0
        )
        uncertain = information_value(
            (0.4, 0.4),
            (((0.5, 1.0), (-1.0, 0.0)), ((-1.0, 0.0), (0.5, 1.0))),
            0.2,
            ((1.0, 0.0), (0.0, 1.0)),
        )
        self.assertFalse(uncertain.exact)
        self.assertGreaterEqual(uncertain.evsi_upper, uncertain.evsi_lower)
        self.assertLessEqual(uncertain.evsi_upper, uncertain.evpi_upper_bound)

    def test_information_shape_probability_and_bounds_negative_cases(self):
        for args in [
            ((0.5,), (((0.0, 0.0),),), 0),
            ((1.0,), (((1.0, -1.0),),), 0),
            ((1.0,), (((0.0, float("inf")),),), 0),
            ((1.0,), (((0.0, 0.0),),), 0, ((0.4,), (0.4,))),
        ]:
            with self.assertRaises(ValueError):
                information_value(*args)
        with self.assertRaises(ValueError):
            PlanningObservationModel(
                context(),
                1,
                ("h",),
                ((0.4,),),
                "sensor",
                "v1",
                "calibration",
                0.1,
                "utility",
                "v1",
            )

    def test_new_frames_do_not_reset_count_cost_or_episode_clock(self):
        episode = PlanningDecisionEpisode(
            context(), "goal", 100, PlanningObservationBudget(2, 0.2, 100)
        )
        for seq in (1, 2):
            self.assertTrue(episode.check_context(context(seq), "goal", 100 + seq))
            self.assertTrue(episode.request_observation(context(seq), 0.1, 100 + seq))
        self.assertTrue(episode.check_context(context(3), "goal", 105))
        self.assertFalse(episode.request_observation(context(3), 0, 105))
        self.assertEqual(episode.usage, (2, 0.2))
        for seq in range(4, 30):
            self.assertTrue(episode.check_context(context(seq), "goal", 105 + seq))
            self.assertFalse(episode.request_observation(context(seq), 0, 105 + seq))
        with self.assertRaises(ValueError):
            episode.reset_scope(context(30), "new-goal", 150)
        timeout = PlanningDecisionEpisode(
            context(), "goal", 100, PlanningObservationBudget(3, 1, 5)
        )
        self.assertTrue(timeout.check_context(context(2), "goal", 105))
        self.assertFalse(timeout.request_observation(context(2), 0.1, 105))

    def test_only_selected_executed_ack_can_reset_budget_and_replay_is_rejected(self):
        episode = PlanningDecisionEpisode(
            context(), "goal", 100, PlanningObservationBudget(1, 1, 100)
        )
        self.assertTrue(episode.request_observation(context(), 0.1, 101))
        self.assertFalse(episode.request_observation(context(), 0.1, 102))
        self.assertTrue(episode.record_selection(context(), "safe"))
        ack = BeliefActionRef("safe", "receipt1", "snapshot1", 3, context())
        self.assertFalse(
            episode.acknowledge_execution(
                replace(ack, execution_status="dispatch_unknown"), 103
            )
        )
        self.assertFalse(
            episode.acknowledge_execution(replace(ack, action_id="other"), 103)
        )
        self.assertTrue(episode.acknowledge_execution(ack, 103))
        self.assertFalse(episode.acknowledge_execution(ack, 104))
        self.assertFalse(episode.check_context(context(), "goal", 104))
        self.assertTrue(episode.check_context(context(2), "goal", 104))
        self.assertTrue(episode.request_observation(context(2), 0.1, 104))

    def test_scope_generation_goal_stale_and_clock_are_checked(self):
        episode = PlanningDecisionEpisode(context(), "goal", 100)
        self.assertFalse(episode.check_context(context(), "different-goal", 101))
        self.assertFalse(episode.check_context(context(action_epoch=4), "goal", 101))
        self.assertFalse(episode.check_context(context(), "goal", 99))
        self.assertTrue(episode.check_context(context(3), "goal", 103))
        self.assertFalse(episode.check_context(context(2), "goal", 104))
        self.assertFalse(
            episode.check_context(replace(context(3), snapshot_id="other"), "goal", 104)
        )
        for stale in (context(action_epoch=2), context(source_generation=0)):
            with self.assertRaises(ValueError):
                episode.reset_scope(stale, "goal", 104)
        episode.reset_scope(context(session_id="nextsession"), "goal", 0)
        self.assertTrue(
            episode.request_observation(context(session_id="nextsession"), 0.1, 1)
        )
