import unittest

from ai_game_player.models import ActionCandidate
from ai_game_player.safety_guard import SafetyGuard, SafetyGuardConfig
from tools.planning_comparison_pacer import ComparisonPacer


class PlanningComparisonPacerTests(unittest.TestCase):
    def test_common_fixture_pacing_preserves_default_rate_burst_and_cycle_limits(self):
        now = [100.0]

        def sleep(seconds):
            now[0] += seconds

        guard = SafetyGuard(
            SafetyGuardConfig(require_target=False, require_foreground=False),
            clock=lambda: now[0],
        )
        pacer = ComparisonPacer(clock=lambda: now[0], sleep=sleep)
        for step in range(180):
            pacer.wait()
            result = guard.check(
                ActionCandidate(
                    f"move{step % 2}",
                    "key",
                    "LEFT" if step % 2 else "RIGHT",
                    confidence=1,
                )
            )
            self.assertTrue(result.allowed, result.reason)
        self.assertAlmostEqual(now[0], 100 + 179 * 0.125)
        self.assertEqual(guard.config.max_actions_per_second, 30)
        self.assertEqual(guard.config.max_burst_actions, 120)
        self.assertEqual(guard.config.max_cycle_repeats, 25)

    def test_unpaced_fixture_is_rejected_by_the_same_guard(self):
        guard = SafetyGuard(
            SafetyGuardConfig(require_target=False, require_foreground=False),
            clock=lambda: 100.0,
        )
        results = [
            guard.check(ActionCandidate(f"move{step % 2}", "key", "LEFT", confidence=1))
            for step in range(31)
        ]
        self.assertEqual(results[-1].code, "rate_limit")
