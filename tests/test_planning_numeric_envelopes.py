from fractions import Fraction
from math import nextafter, inf
import unittest

from ai_game_player.planning_policy_contract import PlanningRiskPolicyConfig
from ai_game_player.planning_policy_projection import project_action_risk
from ai_game_player.planning_result import PlanningActionValue, WeightedValue
from ai_game_player.planning_risk import lower_tail_risk


def action(point, atoms, *, lower=None, upper=None):
    return PlanningActionValue(
        "move",
        point,
        point if lower is None else lower,
        point if upper is None else upper,
        atoms,
        0,
        (),
        point,
    )


class PlanningNumericEnvelopeTests(unittest.TestCase):
    def test_actual_browser_single_point_roundoff_keeps_order_and_point(self):
        point = 0.35679653679653683
        trace_point = 0.3567965367965369
        risk = project_action_risk(
            action(point, (WeightedValue(trace_point, 1),)),
            PlanningRiskPolicyConfig(min_cvar=point),
        )
        self.assertLessEqual(risk.expected_lower, point)
        self.assertGreaterEqual(risk.expected_upper, point)
        self.assertLessEqual(risk.expected_lower, risk.expected_upper)
        self.assertLessEqual(risk.cvar_lower, point)
        self.assertGreaterEqual(risk.cvar_upper, trace_point)
        self.assertFalse(risk.acceptable)
        self.assertTrue(risk.numerical_threshold_indeterminate)

    def test_point_just_outside_intersection_does_not_restore_entire_source_range(self):
        point = 0.5
        for trace_point in (nextafter(point, inf), point + 5e-13):
            with self.subTest(trace_point=trace_point):
                risk = project_action_risk(
                    action(
                        point, (WeightedValue(trace_point, 1),), lower=0.2, upper=0.8
                    ),
                    PlanningRiskPolicyConfig(min_cvar=point),
                )
                self.assertLessEqual(risk.expected_lower, point)
                self.assertGreaterEqual(risk.expected_upper, point)
                self.assertLess(risk.expected_upper - risk.expected_lower, 2e-12)
                self.assertFalse(risk.acceptable)
                self.assertTrue(risk.numerical_threshold_indeterminate)

    def test_substantive_trace_inconsistency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "bounds disagree"):
            project_action_risk(
                action(0.3, (WeightedValue(0.8, 1),)), PlanningRiskPolicyConfig()
            )

    def test_mixed_cvar_threshold_has_conservative_arithmetic_padding(self):
        atoms = ((-0.2, 0.1), (0.4, 0.9))
        raw = lower_tail_risk(atoms, 0, alpha=0.5)
        risk = project_action_risk(
            action(
                0.34, tuple(WeightedValue(v, p) for v, p in atoms), lower=-1, upper=1
            ),
            PlanningRiskPolicyConfig(tail_alpha=0.5, min_cvar=raw.lower_cvar),
        )
        alpha = Fraction.from_float(0.5)
        tail = Fraction.from_float(0.1)
        exact = (
            Fraction.from_float(-0.2) * tail + Fraction.from_float(0.4) * (alpha - tail)
        ) / alpha
        self.assertLessEqual(Fraction.from_float(risk.cvar_lower), exact)
        self.assertGreaterEqual(Fraction.from_float(risk.cvar_upper), exact)
        self.assertFalse(risk.acceptable)
        self.assertTrue(risk.numerical_threshold_indeterminate)

    def test_mixed_catastrophe_sum_at_threshold_is_not_optimistic(self):
        atoms = ((-1.0, 0.1), (-0.95, 0.2), (1.0, 0.7))
        raw = lower_tail_risk(atoms, 0)
        risk = project_action_risk(
            action(
                0.41, tuple(WeightedValue(v, p) for v, p in atoms), lower=-1, upper=1
            ),
            PlanningRiskPolicyConfig(
                min_cvar=-1, max_catastrophe=raw.catastrophic_probability_upper
            ),
        )
        self.assertFalse(risk.acceptable)
        self.assertTrue(risk.numerical_threshold_indeterminate)
        self.assertGreaterEqual(
            Fraction.from_float(risk.catastrophic_upper),
            Fraction.from_float(0.1) + Fraction.from_float(0.2),
        )

    def test_exact_constant_tail_and_zero_catastrophe_remain_inclusive(self):
        risk = project_action_risk(
            action(0.3, (WeightedValue(0.3, 1),)),
            PlanningRiskPolicyConfig(min_cvar=0.3, max_catastrophe=0),
        )
        self.assertTrue(risk.acceptable)
        self.assertEqual(risk.cvar_lower, 0.3)
        self.assertEqual(risk.catastrophic_upper, 0)
        self.assertEqual(risk.numeric_roundoff_guard, 0)


if __name__ == "__main__":
    unittest.main()
