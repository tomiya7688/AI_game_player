import unittest

from ai_game_player.decision_context import EvaluatorEvidence
from ai_game_player.outcome_models import OutcomeEvent
from ai_game_player.self_check import (
    SelfCheckCategory,
    SelfCheckConfig,
    SelfCheckSample,
    SelfCheckSeverity,
    SelfCheckMonitor,
)


def make_sample(
    action_id: str,
    state_signature: str,
    *,
    outcome: str = "changed",
    outcome_confidence: float = 0.9,
    expected_outcome: str | None = None,
    scene_id: str = "unknown-game-scene",
    scene_elapsed_seconds: float = 0.0,
    evaluator_evidence: tuple[EvaluatorEvidence, ...] = (),
) -> SelfCheckSample:
    outcome_event = OutcomeEvent(action_id, outcome, outcome_confidence, False, False, (), f"{outcome} result")
    return SelfCheckSample(
        outcome_event,
        state_signature,
        expected_outcome,
        scene_id,
        scene_elapsed_seconds,
        evaluator_evidence,
    )


class SelfCheckMonitorTest(unittest.TestCase):
    def test_detects_repeated_action_state_cycle_without_game_specific_preset(self):
        monitor = SelfCheckMonitor(
            SelfCheckConfig(history_limit=8, maximum_cycle_period=2, scene_duration_limit_seconds=60.0)
        )
        samples = [
            make_sample("left", "board-1"),
            make_sample("right", "board-2"),
            make_sample("left", "board-1"),
            make_sample("right", "board-2"),
        ]

        events = ()
        for sample in samples:
            events = monitor.observe(sample)

        cycle_event = next(event for event in events if event.category == SelfCheckCategory.REPEATED_CYCLE)
        self.assertEqual(cycle_event.severity, SelfCheckSeverity.WARNING)
        self.assertIn("period=2", cycle_event.evidence)
        self.assertEqual(cycle_event.scene_id, "unknown-game-scene")

    def test_detects_consecutive_no_progress(self):
        monitor = SelfCheckMonitor(
            SelfCheckConfig(history_limit=6, maximum_cycle_period=1, stagnation_limit=3)
        )

        events = ()
        for action_id in ("move-left", "move-right", "wait"):
            events = monitor.observe(make_sample(action_id, "same-state", outcome="unchanged"))

        stagnation_event = next(event for event in events if event.category == SelfCheckCategory.STAGNATION)
        self.assertEqual(stagnation_event.severity, SelfCheckSeverity.WARNING)
        self.assertIn("unchanged_steps=3", stagnation_event.evidence)

    def test_detects_terminal_expected_actual_mismatch_as_critical(self):
        monitor = SelfCheckMonitor()

        events = monitor.observe(
            make_sample("finish-level", "game-over", outcome="failure", expected_outcome="success")
        )

        mismatch = next(event for event in events if event.category == SelfCheckCategory.OUTCOME_MISMATCH)
        self.assertEqual(mismatch.severity, SelfCheckSeverity.CRITICAL)
        self.assertIn("expected=success", mismatch.evidence)
        self.assertIn("actual=failure", mismatch.evidence)
        self.assertEqual(mismatch.to_dict()["schema"], "self-check-event/v1")

    def test_does_not_treat_unknown_actual_outcome_as_a_mismatch(self):
        monitor = SelfCheckMonitor()

        events = monitor.observe(make_sample("continue", "unknown-state", outcome="unknown", expected_outcome="success"))

        self.assertNotIn(SelfCheckCategory.OUTCOME_MISMATCH, {event.category for event in events})

    def test_does_not_raise_terminal_mismatch_from_low_confidence_outcome(self):
        monitor = SelfCheckMonitor()

        events = monitor.observe(
            make_sample(
                "finish-level",
                "unknown-state",
                outcome="failure",
                outcome_confidence=0.2,
                expected_outcome="success",
            )
        )

        self.assertNotIn(SelfCheckCategory.OUTCOME_MISMATCH, {event.category for event in events})

    def test_detects_scene_duration_over_configured_generic_limit(self):
        monitor = SelfCheckMonitor(SelfCheckConfig(scene_duration_limit_seconds=60.0))

        events = monitor.observe(
            make_sample("wait", "scene-state", scene_elapsed_seconds=61.0)
        )

        duration_event = next(event for event in events if event.category == SelfCheckCategory.SCENE_DURATION)
        self.assertEqual(duration_event.severity, SelfCheckSeverity.WARNING)
        self.assertIn("elapsed_seconds=61.0", duration_event.evidence)

    def test_detects_opposing_confident_evaluator_evidence(self):
        monitor = SelfCheckMonitor()
        evidence = (
            EvaluatorEvidence("progress", 0.8, 0.9, 1.0, "state advanced"),
            EvaluatorEvidence("repetition", -0.7, 0.8, 0.9, "action repeated without progress"),
        )

        events = monitor.observe(make_sample("advance", "state-a", evaluator_evidence=evidence))

        disagreement = next(
            event for event in events if event.category == SelfCheckCategory.EVALUATOR_DISAGREEMENT
        )
        self.assertEqual(disagreement.severity, SelfCheckSeverity.WARNING)
        self.assertIn("progress=0.800", disagreement.evidence)
        self.assertIn("repetition=-0.700", disagreement.evidence)

    def test_ignores_disagreement_from_low_reliability_evidence(self):
        monitor = SelfCheckMonitor()
        evidence = (
            EvaluatorEvidence("progress", 0.8, 0.9, 1.0, "state advanced"),
            EvaluatorEvidence("unreliable", -0.9, 0.9, 0.1, "low reliability"),
        )

        events = monitor.observe(make_sample("advance", "state-a", evaluator_evidence=evidence))

        self.assertNotIn(SelfCheckCategory.EVALUATOR_DISAGREEMENT, {event.category for event in events})

    def test_rearms_cycle_event_after_condition_clears(self):
        monitor = SelfCheckMonitor(
            SelfCheckConfig(history_limit=6, maximum_cycle_period=1, stagnation_limit=5)
        )
        first_cycle_events = ()
        for _ in range(2):
            first_cycle_events = monitor.observe(make_sample("same", "state-a"))
        self.assertIn(SelfCheckCategory.REPEATED_CYCLE, {event.category for event in first_cycle_events})

        cleared_events = monitor.observe(make_sample("different", "state-b"))
        self.assertEqual(cleared_events, ())

        second_cycle_events = ()
        for _ in range(2):
            second_cycle_events = monitor.observe(make_sample("same", "state-a"))
        self.assertIn(SelfCheckCategory.REPEATED_CYCLE, {event.category for event in second_cycle_events})

    def test_rolling_history_evicts_old_cycle_evidence(self):
        monitor = SelfCheckMonitor(
            SelfCheckConfig(history_limit=4, maximum_cycle_period=2, stagnation_limit=4)
        )
        events = ()
        for action_id, state_signature in (
            ("left", "state-a"),
            ("right", "state-b"),
            ("left", "state-a"),
            ("right", "state-b"),
        ):
            events = monitor.observe(make_sample(action_id, state_signature))
        self.assertIn(SelfCheckCategory.REPEATED_CYCLE, {event.category for event in events})

        monitor.observe(make_sample("left", "state-c"))
        events = monitor.observe(make_sample("right", "state-d"))

        self.assertNotIn(SelfCheckCategory.REPEATED_CYCLE, {event.category for event in events})

    def test_reset_starts_a_fresh_session_history(self):
        monitor = SelfCheckMonitor(
            SelfCheckConfig(history_limit=4, maximum_cycle_period=1, stagnation_limit=4)
        )
        monitor.observe(make_sample("same", "state-a"))
        monitor.observe(make_sample("same", "state-a"))
        monitor.reset()

        self.assertEqual(monitor.observe(make_sample("same", "state-a")), ())
        cycle_events = monitor.observe(make_sample("same", "state-a"))

        cycle = next(event for event in cycle_events if event.category == SelfCheckCategory.REPEATED_CYCLE)
        self.assertEqual(cycle.step_count, 2)

    def test_rejects_history_capacity_smaller_than_cycle_window(self):
        invalid_cycle_window = {"history_limit": 3, "maximum_cycle_period": 2, "minimum_cycle_repetitions": 2}
        invalid_stagnation_window = {"history_limit": 4, "maximum_cycle_period": 1, "stagnation_limit": 5}
        for values in (invalid_cycle_window, invalid_stagnation_window):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "history limit"):
                SelfCheckConfig(**values)

    def test_rejects_fractional_step_limit(self):
        with self.assertRaisesRegex(ValueError, "must be integers"):
            SelfCheckConfig(history_limit=8, stagnation_limit=2.5)


if __name__ == "__main__":
    unittest.main()
