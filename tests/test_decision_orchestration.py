from pathlib import Path
import tempfile
import unittest

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.decision_orchestrator import DecisionOrchestrator
from ai_game_player.decision_withheld import DecisionDisposition, DecisionWithheld
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.planning_outcome import PlanningOutcome


class Source:
    def __init__(self, *, cached=False, foreign=False):
        self.frame = 1
        self.cached, self.foreign = cached, foreign
        self.fresh_reads = 0

    def read(self):
        return ScreenObservation(
            "game",
            100,
            100,
            features={
                "signature": "stable",
                "capture_metadata": {
                    "frame_id": f"source.{self.frame}",
                    "source_id": "fixture_source",
                    "source_generation": int(self.foreign and self.fresh_reads > 0),
                    "monotonic_ns": self.frame * 1000,
                },
            },
        ), [ActionCandidate("move", "key", "LEFT", confidence=0.95)]

    def read_fresh(self):
        self.fresh_reads += 1
        if not self.cached:
            self.frame += 1
        return self.read()


class Provider:
    def __init__(self, kinds):
        self.kinds = iter(kinds)

    def choose_context(self, context, personality=""):
        kind = next(self.kinds)
        if kind != PlanningOutcome.SELECT:
            raise DecisionWithheld(
                DecisionDisposition(kind, ("test_policy",), "fixture")
            )
        return ActionDecision(
            "move",
            "select allowed movement",
            "fixture",
            context.snapshot_id,
            context.state["screen_id"],
            context.state["signature"],
        )


class Backend:
    def __init__(self):
        self.actions = []

    def execute(self, candidate):
        self.actions.append(candidate)
        return ExecutionResult(candidate.action_id, True, "fixture", "sent")


class DecisionOrchestrationTests(unittest.TestCase):
    def test_fresh_reobservation_is_outside_provider_and_only_one_action_is_executed(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            source, backend = Source(), Backend()
            pipeline = DecisionPipeline(
                source,
                Path(directory),
                Provider([PlanningOutcome.OBSERVE_MORE, PlanningOutcome.SELECT]),
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = backend
            try:
                result = DecisionOrchestrator(pipeline).run(execute=True)
                self.assertTrue(result.executed)
                self.assertEqual(source.fresh_reads, 1)
                self.assertEqual(len(backend.actions), 1)
                self.assertEqual(len(pipeline.execution_history.load()), 1)
                traces = pipeline.engine.trace.recent(10)
                self.assertNotIn("action_id", traces[0])
                self.assertIsNone(traces[0]["decision"])
                self.assertEqual(
                    traces[0]["decision_disposition"]["outcome"], "observe_more"
                )
                self.assertEqual(len(pipeline.engine.trace.recent_actions()), 1)
            finally:
                pipeline.close()

    def test_cached_or_foreign_source_cannot_satisfy_reobservation(self):
        for cached, foreign in ((True, False), (False, True)):
            with (
                self.subTest(cached=cached, foreign=foreign),
                tempfile.TemporaryDirectory() as directory,
            ):
                source, backend = Source(cached=cached, foreign=foreign), Backend()
                pipeline = DecisionPipeline(
                    source,
                    Path(directory),
                    Provider([PlanningOutcome.OBSERVE_MORE, PlanningOutcome.SELECT]),
                )
                pipeline.executor.dry_run = False
                pipeline.executor.live_executor = backend
                try:
                    result = DecisionOrchestrator(pipeline).run(execute=True)
                    self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                    self.assertIn("fresh_capture_not_new", result.reason_codes)
                    self.assertEqual(backend.actions, [])
                    self.assertEqual(pipeline.execution_history.load(), [])
                finally:
                    pipeline.close()

    def test_reobservation_budget_does_not_reset_on_new_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), Provider([PlanningOutcome.OBSERVE_MORE] * 3)
            )
            try:
                orchestrator = DecisionOrchestrator(pipeline, max_reobservations=2)
                result = orchestrator.run()
                self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                self.assertEqual(result.reobservations, 2)
                self.assertEqual(source.fresh_reads, 2)
                self.assertEqual(pipeline.execution_history.load(), [])
                self.assertEqual(len(orchestrator.last_diagnostics), 3)
                self.assertEqual(
                    [item["reobservations"] for item in orchestrator.last_diagnostics],
                    [0, 1, 2],
                )
                self.assertTrue(
                    all(item["reason_codes"] for item in orchestrator.last_diagnostics)
                )
            finally:
                pipeline.close()

    def test_abstain_and_escalate_do_not_request_input_or_another_frame(self):
        for kind in (PlanningOutcome.ABSTAIN, PlanningOutcome.ESCALATE):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                source = Source()
                pipeline = DecisionPipeline(source, Path(directory), Provider([kind]))
                try:
                    result = DecisionOrchestrator(pipeline).run(execute=True)
                    self.assertEqual(result.outcome, kind)
                    self.assertEqual(source.fresh_reads, 0)
                    self.assertEqual(pipeline.execution_history.load(), [])
                    self.assertFalse(pipeline.engine.history.path.exists())
                finally:
                    pipeline.close()

    def test_baseline_withheld_is_a_typed_nonaction_without_rejection_or_fake_action(
        self,
    ):
        class CachedOnly:
            def read(self):
                return Source().read()

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(
                CachedOnly(),
                Path(directory),
                BaselineDecisionProvider(min_recognition=1),
            )
            try:
                result = DecisionOrchestrator(pipeline).run(execute=True)
                self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                self.assertEqual(pipeline.execution_history.load(), [])
                self.assertIsNone(pipeline.engine.last_reliability_result)
                self.assertEqual(
                    pipeline.engine.trace.recent()[0]["decision_disposition"][
                        "outcome"
                    ],
                    "observe_more",
                )
            finally:
                pipeline.close()

    def test_slow_reobservation_cannot_execute_after_total_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            source, backend = Source(), Backend()
            now = [100]
            capture = source.read_fresh

            def slow_capture():
                now[0] += 1000
                return capture()

            source.read_fresh = slow_capture
            pipeline = DecisionPipeline(
                source,
                Path(directory),
                Provider([PlanningOutcome.OBSERVE_MORE, PlanningOutcome.SELECT]),
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = backend
            try:
                result = DecisionOrchestrator(
                    pipeline, timeout_ns=1000, clock_ns=lambda: now[0]
                ).run(execute=True)
                self.assertEqual(result.outcome, PlanningOutcome.ABSTAIN)
                self.assertEqual(backend.actions, [])
                self.assertEqual(source.fresh_reads, 1)
            finally:
                pipeline.close()

    def test_session_nonselect_is_stopped_without_success_or_failure(self):
        from ai_game_player.game_session import (
            GameSessionController,
            SessionStatus,
            SessionStep,
        )
        from test_game_session import FakeRuntime

        runtime = FakeRuntime(1)
        controller = GameSessionController(
            lambda: runtime,
            lambda _runtime, _command: SessionStep(withheld_reason="decision: abstain"),
        )
        controller.step("execute")
        self.assertEqual(controller.status, SessionStatus.STOPPED)
        self.assertEqual(runtime.close_count, 1)
        self.assertFalse(controller.run_control.is_running)
