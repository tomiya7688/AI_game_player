from pathlib import Path
import tempfile
import unittest

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.decision_context import DecisionContextBuilder, DecisionTraceStore
from ai_game_player.execution_receipt import ExecutionReceipt
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.run_control import ExecutionCancelled
from ai_game_player.game_session import (
    GameSessionController,
    LoopObservation,
    SessionStatus,
    SessionStep,
)
from unittest.mock import patch


def observation(signature, progress=0):
    return ScreenObservation(
        "game",
        200,
        120,
        features={"signature": signature, "state": {"progress": progress}},
    )


class Source:
    def __init__(self):
        self.observation = observation("before")
        self.candidates = [
            ActionCandidate("a", "key", "LEFT", confidence=0.95),
            ActionCandidate("b", "key", "RIGHT", confidence=0.9),
        ]

    def read(self):
        return self.observation, self.candidates


class SequenceProvider:
    def __init__(self, actions):
        self.actions = iter(actions)

    def choose_context(self, context, personality=""):
        return ActionDecision(
            next(self.actions),
            "select allowed action",
            "test",
            context.snapshot_id,
            context.state["screen_id"],
            context.state["signature"],
        )


class SuccessBackend:
    def execute(self, candidate):
        return ExecutionResult(candidate.action_id, True, "live", "sent")


class ExecutionOutcomeAttributionTests(unittest.TestCase):
    def test_decision_only_observation_cannot_create_actual_action_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = GamePlayerEngine(Path(directory))
            candidate = ActionCandidate("a", "key", "LEFT")
            engine.step(observation("before"), [candidate])
            engine.step(observation("after", 1), [candidate])
            self.assertIsNone(engine.last_outcome_event)
            self.assertIsNone(engine._previous_action_id)
            self.assertFalse(
                any("action_outcome" in row for row in engine.trace.recent(10))
            )

    def test_dry_run_observation_cannot_create_actual_action_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a", "b"])
            )
            try:
                self.assertFalse(pipeline.run_and_execute().executed)
                source.observation = observation("after", 1)
                pipeline.run()
                self.assertIsNone(pipeline.engine.last_outcome_event)
                self.assertIsNone(pipeline.engine._previous_action_id)
            finally:
                pipeline.close()

    def test_actual_action_then_planned_decision_preserves_actual_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a", "b"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                self.assertTrue(pipeline.run_and_execute().executed)
                source.observation = observation("after", 1)
                pipeline.run()
                self.assertEqual(pipeline.engine._previous_action_id, "a")
            finally:
                pipeline.close()

    def test_terminal_post_observation_publishes_last_actual_action_once_without_next_decision(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                after = ScreenObservation(
                    "game",
                    200,
                    120,
                    ["GAME OVER"],
                    {"signature": "terminal", "state": {"progress": 1}},
                )
                with patch.object(
                    pipeline.engine.trace,
                    "record_action_outcome",
                    wraps=pipeline.engine.trace.record_action_outcome,
                ) as record:
                    observed = pipeline.complete_execution_observation(after)
                    repeated = pipeline.complete_execution_observation(after)
                self.assertIs(observed, repeated)
                self.assertEqual(record.call_count, 1)
                pipeline.engine.record_execution_receipt(
                    source.observation, observed.receipt
                )
                self.assertIs(pipeline.complete_execution_observation(after), observed)
                self.assertEqual(observed.receipt.action_id, "a")
                self.assertEqual(observed.outcome.status, "failure")
                self.assertEqual(
                    observed.receipt.action_generation, pipeline.controller.rearm_token
                )
                rows = pipeline.engine.trace.recent(10)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["action_outcome"]["status"], "failure")
            finally:
                pipeline.close()

    def test_before_dispatch_failure_keeps_prior_actual_pending_and_has_no_b_outcome(
        self,
    ):
        class BeforeDispatchFailure:
            input_dispatch_started = False

            def execute(self, candidate):
                raise RuntimeError("target validation failed")

        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a", "b"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                original = pipeline.engine._pending_execution_receipt
                source.observation = observation("after-a", 1)
                pipeline.executor.live_executor = BeforeDispatchFailure()
                with self.assertRaisesRegex(RuntimeError, "target validation"):
                    pipeline.run_and_execute()
                self.assertIs(pipeline.engine._pending_execution_receipt, original)
                self.assertEqual(pipeline.engine._previous_action_id, "a")
                rows = pipeline.engine.trace.recent(10)
                self.assertEqual(rows[0]["action_outcome"]["action_id"], "a")
                self.assertEqual(
                    rows[1]["execution_receipt"]["execution_status"], "not_executed"
                )
                self.assertFalse(rows[1]["execution_receipt"]["input_dispatch_started"])
                self.assertNotIn("action_outcome", rows[1])
                self.assertEqual(
                    [
                        row["action_id"]
                        for row in pipeline.engine.trace.recent_actions()
                    ],
                    ["a"],
                )
            finally:
                pipeline.close()

    def test_replayed_older_receipt_cannot_replace_new_actual_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a", "b"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                first = pipeline.engine._pending_execution_receipt
                before_first = source.observation
                source.observation = observation("after-a", 1)
                pipeline.run_and_execute()
                second = pipeline.engine._pending_execution_receipt
                pipeline.engine.record_execution_receipt(before_first, first)
                self.assertIs(pipeline.engine._pending_execution_receipt, second)
                self.assertEqual(second.action_id, "b")
                self.assertIsNone(pipeline.engine.last_execution_observation)
            finally:
                pipeline.close()

    def test_backend_exception_without_dispatch_evidence_stays_unknown(self):
        class UnknownBackend:
            def execute(self, candidate):
                raise RuntimeError("backend result unavailable")

        for dispatch_state in (None, True):
            backend = UnknownBackend()
            if dispatch_state is not None:
                backend.input_dispatch_started = dispatch_state
            with (
                self.subTest(dispatch=dispatch_state),
                tempfile.TemporaryDirectory() as directory,
            ):
                source = Source()
                pipeline = DecisionPipeline(
                    source, Path(directory), SequenceProvider(["a"])
                )
                pipeline.executor.dry_run = False
                pipeline.executor.live_executor = backend
                try:
                    with self.assertRaisesRegex(RuntimeError, "backend result"):
                        pipeline.run_and_execute()
                    observed = pipeline.complete_execution_observation(
                        observation("changed", 100)
                    )
                    self.assertEqual(
                        observed.receipt.execution_status, "dispatch_unknown"
                    )
                    self.assertIs(
                        observed.receipt.input_dispatch_started, dispatch_state
                    )
                    self.assertFalse(observed.receipt.eligible_for_learning)
                    self.assertEqual(observed.outcome.status, "unknown")
                    self.assertEqual(observed.outcome.confidence, 0)
                    self.assertTrue(observed.outcome.abstained)
                    self.assertEqual(pipeline.engine.trace.recent_actions(), [])
                finally:
                    pipeline.close()

    def test_stop_and_rearm_during_dispatch_records_only_ineligible_old_generation_audit(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a"])
            )

            class StopBackend:
                def execute(self, candidate):
                    pipeline.controller.stop("user stop")
                    pipeline.controller.start()
                    return ExecutionResult(
                        candidate.action_id, True, "live", "sent before stop"
                    )

            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = StopBackend()
            old_generation = pipeline.controller.rearm_token
            try:
                with self.assertRaises(ExecutionCancelled):
                    pipeline.run_and_execute()
                receipt = pipeline.engine.trace.recent(1)[0]["execution_receipt"]
                self.assertEqual(receipt["action_generation"], old_generation)
                self.assertEqual(receipt["execution_status"], "executed")
                self.assertFalse(receipt["eligible_for_learning"])
                self.assertIsNone(pipeline.engine._pending_execution_receipt)
                self.assertIsNone(
                    pipeline.complete_execution_observation(
                        observation("new-generation", 2)
                    )
                )
                self.assertEqual(pipeline.engine.trace.recent_actions(), [])
                self.assertTrue(pipeline.load_execution_history()[0].executed)
            finally:
                pipeline.close()

    def test_execution_history_failure_does_not_lose_real_dispatch_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                with patch.object(
                    pipeline.execution_history,
                    "append",
                    side_effect=OSError("history unavailable"),
                ):
                    with self.assertRaisesRegex(OSError, "history unavailable"):
                        pipeline.run_and_execute()
                pair = pipeline.complete_execution_observation(observation("after", 1))
                self.assertEqual(pair.receipt.action_id, "a")
                self.assertEqual(pair.receipt.execution_status, "executed")
                self.assertIn("action_outcome", pipeline.engine.trace.recent()[0])
            finally:
                pipeline.close()

    def test_stop_and_rearm_during_outcome_detection_cannot_publish_old_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                original_detect = pipeline.engine.outcome_detector.detect

                def detect(before, action_id, after):
                    event = original_detect(before, action_id, after)
                    pipeline.controller.stop("stop while observing")
                    pipeline.controller.start()
                    return event

                with patch.object(
                    pipeline.engine.outcome_detector, "detect", side_effect=detect
                ):
                    with self.assertRaises(ExecutionCancelled):
                        pipeline.complete_execution_observation(observation("after", 1))
                self.assertNotIn("action_outcome", pipeline.engine.trace.recent(1)[0])
                self.assertIsNone(pipeline.engine.last_execution_observation)
                self.assertFalse(pipeline.engine._pending_outcome_observed)
                self.assertIsNone(
                    pipeline.complete_execution_observation(observation("new", 2))
                )
            finally:
                pipeline.close()

    def test_next_provider_failure_does_not_lose_completed_actual_observation(self):
        class FailingProvider(SequenceProvider):
            def choose_context(self, context, personality=""):
                if hasattr(self, "called"):
                    raise ValueError("provider failed")
                self.called = True
                return super().choose_context(context, personality)

        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(source, Path(directory), FailingProvider(["a"]))
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                source.observation = observation("after-a", 1)
                with self.assertRaisesRegex(ValueError, "provider failed"):
                    pipeline.run_and_execute()
                self.assertEqual(
                    pipeline.engine.trace.recent(1)[0]["action_outcome"]["action_id"],
                    "a",
                )
                self.assertEqual(
                    pipeline.engine.last_execution_observation.receipt.action_id, "a"
                )
            finally:
                pipeline.close()

    def test_pending_b_does_not_inherit_a_outcome_in_history_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a", "b"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                source.observation = observation("after-a", 1)
                pipeline.run_and_execute()
                rows = pipeline.engine.trace.recent(10)
                self.assertIn("action_outcome", rows[0])
                self.assertNotIn("action_outcome", rows[1])
                context = pipeline.engine.context_builder.build(
                    source.observation,
                    source.candidates,
                    source.candidates,
                    recent_history=pipeline.engine.trace.recent_actions(),
                )
                self.assertEqual(context.recent_history[-1]["action_id"], "b")
                self.assertEqual(context.recent_history[-1]["outcome"], "unknown")
                self.assertFalse(context.recent_history[-1]["state_changed"])
            finally:
                pipeline.close()

    def test_session_terminal_observation_records_last_action_before_runtime_close(
        self,
    ):
        class Scheduler:
            def __init__(self):
                self.pending = []

            def schedule(self, delay_ms, callback):
                self.pending.append(callback)
                return callback

            def cancel(self, token):
                if token in self.pending:
                    self.pending.remove(token)

            def run_next(self):
                self.pending.pop(0)()

        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            scheduler = Scheduler()
            runtimes = []

            def factory():
                pipeline = DecisionPipeline(
                    source,
                    Path(directory),
                    SequenceProvider(["a"]),
                    controller=controller.run_control,
                )
                pipeline.executor.dry_run = False
                pipeline.executor.live_executor = SuccessBackend()
                runtimes.append(pipeline)
                return pipeline

            def step(runtime, command):
                runtime.run_and_execute()
                source.observation = ScreenObservation(
                    "game",
                    200,
                    120,
                    ["GAME OVER"],
                    {"signature": "terminal", "state": {"progress": 1}},
                )
                return SessionStep()

            def observe():
                return LoopObservation(
                    source.observation,
                    outcome_status="failure"
                    if source.observation.ocr_text
                    else "ongoing",
                )

            controller = GameSessionController(
                factory, step, scheduler=scheduler, loop_observer=observe
            )
            controller.start_loop(interval_ms=1)
            scheduler.run_next()
            scheduler.run_next()
            self.assertEqual(controller.status, SessionStatus.COMPLETED)
            rows = runtimes[0].engine.trace.recent(10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action_outcome"]["status"], "failure")
            self.assertEqual(
                rows[0]["action_outcome"]["snapshot_id"], rows[0]["snapshot_id"]
            )
            self.assertEqual(
                rows[0]["action_outcome"]["action_generation"],
                rows[0]["execution_receipt"]["action_generation"],
            )

    def test_rearm_does_not_project_old_completed_outcome_into_new_context(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a", "b"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                pipeline.complete_execution_observation(observation("after", 1))
                pipeline.controller.stop()
                pipeline.controller.start()
                source.observation = observation("new", 2)
                pipeline.run()
                latest = pipeline.engine.trace.recent(1)[0]["context"]
                self.assertEqual(latest["previous_outcome"]["status"], "unknown")
                self.assertEqual(latest["recent_history"], [])
            finally:
                pipeline.close()

    def test_staged_new_decision_preserves_receipt_audit_published_before_commit(self):
        source = Source()
        first = DecisionContextBuilder().build(
            source.observation, source.candidates, source.candidates
        )
        second = DecisionContextBuilder().build(
            source.observation, source.candidates, source.candidates
        )
        with tempfile.TemporaryDirectory() as directory:
            store = DecisionTraceStore(Path(directory) / "trace.json")
            store.append(first, ActionDecision("a", "planned", "test"))
            staged = store.prepare_append(
                second, ActionDecision("b", "planned", "test")
            )
            try:
                receipt = ExecutionReceipt(
                    first.snapshot_id,
                    "a",
                    0,
                    source.observation.screen_id,
                    source.observation.features["signature"],
                    "executed",
                    True,
                    eligible_for_learning=False,
                )
                self.assertTrue(store.record_execution_receipt(receipt))
                staged.publish()
            finally:
                staged.discard()
            rows = store.recent(10)
            self.assertEqual([row["action_id"] for row in rows], ["a", "b"])
            self.assertEqual(rows[0]["execution_receipt"], receipt.to_dict())
            self.assertEqual(store.recent_actions(), [])

    def test_cached_completed_outcome_still_checks_current_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Source()
            pipeline = DecisionPipeline(
                source, Path(directory), SequenceProvider(["a"])
            )
            pipeline.executor.dry_run = False
            pipeline.executor.live_executor = SuccessBackend()
            try:
                pipeline.run_and_execute()
                after = observation("after", 1)
                pipeline.complete_execution_observation(after)
                original = pipeline.controller.ensure_running

                def ensure_then_rearm(token):
                    original(token)
                    pipeline.controller.stop()
                    pipeline.controller.start()

                with patch.object(
                    pipeline.controller, "ensure_running", side_effect=ensure_then_rearm
                ):
                    with self.assertRaises(ExecutionCancelled):
                        pipeline.complete_execution_observation(after)
            finally:
                pipeline.close()


if __name__ == "__main__":
    unittest.main()
