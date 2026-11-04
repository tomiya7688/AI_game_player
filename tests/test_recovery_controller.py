import random
import tempfile
import unittest
from pathlib import Path

from ai_game_player.event_journal import EventJournal
from ai_game_player.game_session import (
    GameSessionController,
    SessionStatus,
    SessionStep,
)
from ai_game_player.recovery_controller import RecoveryController
from ai_game_player.run_control import ExecutionCancelled
from ai_game_player.recovery_models import (
    RecoveryAction,
    RecoveryAttemptResult,
    RecoveryAttemptStatus,
    RecoveryPolicy,
    RecoveryReason,
    RecoveryState,
)
from ai_game_player.recovery_session_adapter import RecoverySessionAdapter
from ai_game_player.self_check_models import (
    SelfCheckCategory,
    SelfCheckEvent,
    SelfCheckSeverity,
)


def make_event(
    step: int,
    *,
    severity: SelfCheckSeverity = SelfCheckSeverity.WARNING,
    scene: str = "scene-a",
    category: SelfCheckCategory = SelfCheckCategory.REPEATED_CYCLE,
) -> SelfCheckEvent:
    return SelfCheckEvent(category, severity, step, scene, "anomaly", ("evidence",))


def result_for(
    plan, snapshot_id: str, status=RecoveryAttemptStatus.UNRESOLVED, detail: str = ""
):
    return RecoveryAttemptResult(
        plan.action, plan.action_epoch, plan.attempt, snapshot_id, status, detail
    )


class RecoveryControllerTest(unittest.TestCase):
    def setUp(self):
        self.controller = RecoveryController()

    def event_plan(
        self,
        step,
        snapshot="frame-1",
        now=0.0,
        generation=0,
        stop=False,
        **event_options,
    ):
        return self.controller.on_event(
            make_event(step, **event_options),
            snapshot_id=snapshot,
            action_generation=generation,
            now_monotonic=now,
            stop_requested=stop,
        )

    def test_warning_reobserves_then_redecides_on_new_frame_and_resolves(self):
        plan = self.event_plan(2)
        self.assertEqual(
            (plan.action, plan.reason, plan.attempt),
            (RecoveryAction.REOBSERVE, RecoveryReason.WARNING_REOBSERVE, 1),
        )
        self.assertEqual(plan.remaining.reobserve, 1)
        self.assertEqual(plan.deadline, 3.0)

        plan = self.controller.ack(result_for(plan, "frame-2"), now_monotonic=0.5)
        self.assertEqual(
            (plan.action, plan.snapshot_id, plan.attempt),
            (RecoveryAction.REDECIDE, "frame-2", 2),
        )
        self.assertEqual(self.controller.state, RecoveryState.REDECIDE)

        plan = self.controller.ack(
            result_for(plan, "frame-3", RecoveryAttemptStatus.RESOLVED),
            now_monotonic=1.0,
        )
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.RESOLVED),
        )
        self.assertEqual(plan.event_ref, "repeated_cycle@2")
        self.assertEqual(self.controller.state, RecoveryState.RESOLVED)

    def test_two_stale_reobserves_abstain_then_repeated_scene_failure_requests_stop(
        self,
    ):
        plan = self.event_plan(2)
        plan = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.1)
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.REOBSERVE, RecoveryReason.STALE_FRAME_REOBSERVE),
        )
        plan = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.2)
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.BUDGET_EXHAUSTED),
        )

        self.assertEqual(self.event_plan(3, now=0.3).reason, RecoveryReason.COOLDOWN)
        plan = self.event_plan(4, now=0.4)
        plan = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.5)
        plan = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.6)
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.REQUEST_STOP, RecoveryReason.SCENE_FAILURES_EXCEEDED),
        )
        self.assertIn("budget_exhausted", plan.stop_reason)

    def test_critical_requests_stop_and_stays_latched_until_rearm(self):
        plan = self.event_plan(5, severity=SelfCheckSeverity.CRITICAL)
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.REQUEST_STOP, RecoveryReason.CRITICAL_SELF_CHECK),
        )
        again = self.event_plan(6)
        self.assertEqual(
            (again.action, again.reason),
            (RecoveryAction.REQUEST_STOP, RecoveryReason.STOP_ALREADY_REQUESTED),
        )
        self.assertEqual(again.stop_reason, plan.stop_reason)

        rearmed = self.event_plan(7, generation=1)
        self.assertEqual(
            (rearmed.action, rearmed.action_epoch), (RecoveryAction.REOBSERVE, 1)
        )

    def test_ack_from_unissued_generation_does_not_clear_stop_latch(self):
        self.event_plan(5, severity=SelfCheckSeverity.CRITICAL)
        forged = self.controller.ack(
            RecoveryAttemptResult(
                RecoveryAction.REOBSERVE,
                1,
                1,
                "frame-9",
                RecoveryAttemptStatus.RESOLVED,
            ),
            now_monotonic=0.1,
        )
        self.assertEqual(forged.reason, RecoveryReason.UNEXPECTED_ACK)
        self.assertEqual(self.controller.state, RecoveryState.STOP_REQUESTED)
        self.assertEqual(
            self.event_plan(6, now=0.2).action, RecoveryAction.REQUEST_STOP
        )

    def test_existing_stop_is_never_replaced_and_blocks_recovery_until_rearm(self):
        plan = self.event_plan(2)
        stopped = self.controller.ack(
            result_for(plan, "frame-2"), now_monotonic=0.1, stop_requested=True
        )
        self.assertEqual(
            (stopped.action, stopped.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.STOP_LATCHED),
        )
        self.assertIsNone(stopped.stop_reason)
        after = self.event_plan(3, now=0.2)
        self.assertEqual(
            (after.action, after.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.STOP_LATCHED),
        )
        self.assertEqual(
            self.event_plan(4, now=0.3, generation=1).action, RecoveryAction.REOBSERVE
        )

    def test_scene_change_resets_failures_and_cooldown(self):
        plan = self.event_plan(2)
        plan = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.1)
        self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.2)
        plan = self.event_plan(3, now=0.3, scene="scene-b")
        self.assertEqual(
            (plan.action, plan.scene_id), (RecoveryAction.REOBSERVE, "scene-b")
        )
        plan = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.4)
        plan = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=0.5)
        self.assertEqual(plan.action, RecoveryAction.ABSTAIN)

    def test_redecide_is_not_repeated_on_the_same_frame(self):
        controller = RecoveryController(RecoveryPolicy(max_reobserve=2, max_redecide=3))
        plan = controller.on_event(
            make_event(2),
            snapshot_id="frame-1",
            action_generation=0,
            now_monotonic=0.0,
            stop_requested=False,
        )
        plan = controller.ack(result_for(plan, "frame-2"), now_monotonic=0.1)
        self.assertEqual(
            (plan.action, plan.snapshot_id), (RecoveryAction.REDECIDE, "frame-2")
        )
        plan = controller.ack(result_for(plan, "frame-2"), now_monotonic=0.2)
        self.assertEqual(plan.action, RecoveryAction.REOBSERVE)
        plan = controller.ack(result_for(plan, "frame-2"), now_monotonic=0.3)
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.BUDGET_EXHAUSTED),
        )

    def test_resolved_without_new_frame_is_not_counted_as_recovered(self):
        plan = self.event_plan(2)
        plan = self.controller.ack(
            result_for(plan, "frame-1", RecoveryAttemptStatus.RESOLVED),
            now_monotonic=0.1,
        )
        self.assertEqual(plan.action, RecoveryAction.REOBSERVE)
        self.assertNotEqual(self.controller.state, RecoveryState.RESOLVED)

    def test_duplicate_event_and_pending_plan_do_not_progress(self):
        first = self.event_plan(2)
        duplicate = self.event_plan(2, now=0.1)
        self.assertEqual(
            (duplicate.action, duplicate.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.DUPLICATE_EVENT),
        )
        pending = self.event_plan(3, now=0.2)
        self.assertEqual(pending.reason, RecoveryReason.PLAN_PENDING)
        self.assertEqual(pending.remaining.reobserve, first.remaining.reobserve)

    def test_older_step_is_refused_after_leaving_the_duplicate_window(self):
        controller = RecoveryController(RecoveryPolicy(cooldown_steps=0))
        for step in range(1, 70):
            controller.on_event(
                make_event(
                    step, category=SelfCheckCategory.STAGNATION, scene=f"scene-{step}"
                ),
                snapshot_id=f"frame-{step}",
                action_generation=0,
                now_monotonic=float(step) * 10,
                stop_requested=False,
            )
        replay = controller.on_event(
            make_event(1, category=SelfCheckCategory.STAGNATION, scene="scene-1"),
            snapshot_id="frame-1",
            action_generation=0,
            now_monotonic=1000.0,
            stop_requested=False,
        )
        self.assertEqual(
            (replay.action, replay.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.DUPLICATE_EVENT),
        )

    def test_redecide_resolution_on_its_own_input_frame_is_not_trusted(self):
        plan = self.event_plan(2)
        plan = self.controller.ack(result_for(plan, "frame-2"), now_monotonic=0.1)
        self.assertEqual(plan.action, RecoveryAction.REDECIDE)
        plan = self.controller.ack(
            result_for(plan, "frame-2", RecoveryAttemptStatus.RESOLVED),
            now_monotonic=0.2,
        )
        self.assertNotEqual(plan.reason, RecoveryReason.RESOLVED)

    def test_deadline_boundary_is_exclusive(self):
        plan = self.event_plan(2)
        before = self.controller.ack(result_for(plan, "frame-1"), now_monotonic=2.999)
        self.assertEqual(before.action, RecoveryAction.REOBSERVE)
        at_deadline = self.controller.ack(
            result_for(before, "frame-2"), now_monotonic=3.0
        )
        self.assertEqual(
            (at_deadline.action, at_deadline.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.DEADLINE_EXCEEDED),
        )

    def test_resolved_after_deadline_is_a_deadline_failure(self):
        plan = self.event_plan(2)
        late = self.controller.ack(
            result_for(plan, "frame-2", RecoveryAttemptStatus.RESOLVED),
            now_monotonic=3.0,
        )
        self.assertEqual(
            (late.action, late.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.DEADLINE_EXCEEDED),
        )

    def test_correlation_ids_stay_unique_across_reset_and_critical_events(self):
        first = self.event_plan(2)
        self.controller.reset()
        second = self.event_plan(2)
        self.assertEqual(first.event_ref, second.event_ref)
        self.assertNotEqual(first.correlation_id, second.correlation_id)
        critical = self.event_plan(9, now=0.1, severity=SelfCheckSeverity.CRITICAL)
        self.assertEqual(critical.event_ref, "repeated_cycle@9")
        self.assertEqual(critical.episode_id, 3)
        self.assertIn(":3:scene-a:repeated_cycle@9", critical.correlation_id)

    def test_hung_worker_is_cut_off_by_poll_and_late_ack_is_ignored(self):
        plan = self.event_plan(2)
        self.assertIsNone(self.controller.poll(now_monotonic=2.9))
        cut = self.controller.poll(now_monotonic=3.0)
        self.assertEqual(cut.reason, RecoveryReason.DEADLINE_EXCEEDED)
        late = self.controller.ack(result_for(plan, "frame-2"), now_monotonic=9.0)
        self.assertEqual(late.reason, RecoveryReason.UNEXPECTED_ACK)

    def test_new_event_after_deadline_with_pending_plan_fails_episode(self):
        self.event_plan(2)
        plan = self.event_plan(3, now=3.5)
        self.assertEqual(plan.reason, RecoveryReason.DEADLINE_EXCEEDED)

    def test_no_legal_candidate_failure_stays_bounded(self):
        plan = self.event_plan(2)
        plan = self.controller.ack(result_for(plan, "frame-2"), now_monotonic=0.1)
        self.assertEqual(plan.action, RecoveryAction.REDECIDE)
        plan = self.controller.ack(
            result_for(
                plan, "frame-2", RecoveryAttemptStatus.FAILED, "no_allowed_candidates"
            ),
            now_monotonic=0.2,
        )
        self.assertEqual(plan.action, RecoveryAction.REOBSERVE)
        plan = self.controller.ack(result_for(plan, "frame-3"), now_monotonic=0.3)
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.BUDGET_EXHAUSTED),
        )

    def test_stale_epoch_target_loss_and_unexpected_ack_are_refused(self):
        self.event_plan(2, generation=3)
        stale = self.event_plan(3, generation=2, now=0.1)
        self.assertEqual(stale.reason, RecoveryReason.STALE_EPOCH)
        lost = self.controller.on_event(
            make_event(4),
            snapshot_id="frame-1",
            action_generation=3,
            now_monotonic=0.2,
            stop_requested=False,
            target_lost=True,
        )
        self.assertEqual(
            (lost.action, lost.reason),
            (RecoveryAction.ABSTAIN, RecoveryReason.TARGET_LOST),
        )
        unexpected = self.controller.ack(
            RecoveryAttemptResult(
                RecoveryAction.REOBSERVE,
                3,
                1,
                "frame-1",
                RecoveryAttemptStatus.RESOLVED,
            ),
            now_monotonic=0.3,
        )
        self.assertEqual(unexpected.reason, RecoveryReason.UNEXPECTED_ACK)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.event_plan(2, now=float("nan"))
        self.event_plan(2, now=1.0)
        with self.assertRaises(ValueError):
            self.event_plan(3, now=0.5)
        with self.assertRaises(ValueError):
            self.event_plan(4, now=1.0, snapshot=" ")
        with self.assertRaises(ValueError):
            self.event_plan(5, now=1.0, generation=-1)
        for options in (
            {"max_reobserve": -1},
            {"max_redecide": True},
            {"deadline_seconds": 0.0},
            {"deadline_seconds": float("inf")},
            {"deadline_seconds": 61.0},
            {"max_scene_failures": 0},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                RecoveryPolicy(**options)
        with self.assertRaises(ValueError):
            RecoveryAttemptResult(
                RecoveryAction.ABSTAIN, 0, 1, "frame", RecoveryAttemptStatus.FAILED
            )

    def test_zero_budget_policy_fails_immediately_without_attempts(self):
        controller = RecoveryController(
            RecoveryPolicy(max_reobserve=0, max_redecide=0, max_scene_failures=1)
        )
        plan = controller.on_event(
            make_event(2),
            snapshot_id="frame-1",
            action_generation=0,
            now_monotonic=0.0,
            stop_requested=False,
        )
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.REQUEST_STOP, RecoveryReason.SCENE_FAILURES_EXCEEDED),
        )

    def test_random_sequences_stay_bounded_and_never_act_after_stop(self):
        policy = RecoveryPolicy()
        attempt_limit = policy.max_reobserve + policy.max_redecide
        for seed in range(20):
            rng = random.Random(seed)
            controller = RecoveryController(policy)
            now, generation, step, stop = 0.0, 0, 0, False
            pending = None
            attempts_in_episode = 0
            for _ in range(500):
                now += rng.choice((0.0, 0.1, 0.5, 2.0))
                if rng.random() < 0.02:
                    generation += 1
                    stop = False
                if rng.random() < 0.05:
                    stop = True
                if pending is not None and rng.random() < 0.7:
                    status = rng.choice(list(RecoveryAttemptStatus))
                    snapshot = rng.choice(
                        (pending.snapshot_id, f"frame-{step}-{rng.random()}")
                    )
                    plan = controller.ack(
                        RecoveryAttemptResult(
                            pending.action,
                            pending.action_epoch,
                            pending.attempt,
                            snapshot,
                            status,
                        ),
                        now_monotonic=now,
                        stop_requested=stop,
                    )
                else:
                    step += 1
                    severity = (
                        SelfCheckSeverity.CRITICAL
                        if rng.random() < 0.03
                        else SelfCheckSeverity.WARNING
                    )
                    plan = controller.on_event(
                        make_event(
                            step, severity=severity, scene=rng.choice(("a", "b"))
                        ),
                        snapshot_id=f"frame-{step}",
                        action_generation=generation,
                        now_monotonic=now,
                        stop_requested=stop,
                    )
                if plan.action in (RecoveryAction.REOBSERVE, RecoveryAction.REDECIDE):
                    self.assertFalse(stop, f"seed={seed}")
                    attempts_in_episode = plan.attempt
                    self.assertLessEqual(attempts_in_episode, attempt_limit)
                    pending = plan
                elif plan.reason is not RecoveryReason.PLAN_PENDING:
                    pending = None


class RecoverySessionAdapterTest(unittest.TestCase):
    class FakeJournal:
        def __init__(self, fail_on: int | None = None) -> None:
            self.records = []
            self.calls = 0
            self.fail_on = fail_on

        def append(self, event_type, **fields):
            self.calls += 1
            if self.fail_on is not None and self.calls >= self.fail_on:
                raise OSError("disk full")
            self.records.append((event_type, fields))

    def test_plans_and_attempts_are_journaled_with_correlation(self):
        with tempfile.TemporaryDirectory() as directory:
            with EventJournal(
                Path(directory) / "journal.sqlite3", session_id="s"
            ) as journal:
                stops = []
                adapter = RecoverySessionAdapter(
                    RecoveryController(), journal, stops.append
                )
                plan = adapter.handle_event(
                    make_event(2),
                    snapshot_id="frame-1",
                    action_generation=0,
                    now_monotonic=0.0,
                    stop_requested=False,
                )
                next_plan = adapter.handle_result(
                    result_for(plan, "frame-2"),
                    correlation_id=plan.correlation_id,
                    now_monotonic=0.1,
                )
                self.assertEqual(next_plan.action, RecoveryAction.REDECIDE)
                self.assertEqual(stops, [])
                self.assertEqual(journal.last_sequence, 3)

    def test_journal_failure_fails_closed_with_stop_request(self):
        journal = self.FakeJournal(fail_on=1)
        stops = []
        adapter = RecoverySessionAdapter(RecoveryController(), journal, stops.append)
        plan = adapter.handle_event(
            make_event(2),
            snapshot_id="frame-1",
            action_generation=0,
            now_monotonic=0.0,
            stop_requested=False,
        )
        self.assertEqual(
            (plan.action, plan.reason),
            (RecoveryAction.REQUEST_STOP, RecoveryReason.JOURNAL_FAILED),
        )
        self.assertEqual(stops, [plan.stop_reason])

    def test_attempt_journal_failure_fails_closed(self):
        journal = self.FakeJournal(fail_on=2)
        stops = []
        adapter = RecoverySessionAdapter(RecoveryController(), journal, stops.append)
        plan = adapter.handle_event(
            make_event(2),
            snapshot_id="frame-1",
            action_generation=0,
            now_monotonic=0.0,
            stop_requested=False,
        )
        plan = adapter.handle_result(
            result_for(plan, "frame-2"),
            correlation_id=plan.correlation_id,
            now_monotonic=0.1,
        )
        self.assertEqual(plan.reason, RecoveryReason.JOURNAL_FAILED)
        self.assertEqual(len(stops), 1)

    def test_existing_stop_is_not_requested_again(self):
        journal = self.FakeJournal()
        stops = []
        adapter = RecoverySessionAdapter(RecoveryController(), journal, stops.append)
        plan = adapter.handle_event(
            make_event(2, severity=SelfCheckSeverity.CRITICAL),
            snapshot_id="frame-1",
            action_generation=0,
            now_monotonic=0.0,
            stop_requested=True,
        )
        self.assertEqual(plan.reason, RecoveryReason.STOP_LATCHED)
        self.assertEqual(stops, [])

    def test_hung_attempt_poll_requests_stop_after_repeated_scene_failure(self):
        journal = self.FakeJournal()
        stops = []
        adapter = RecoverySessionAdapter(
            RecoveryController(RecoveryPolicy(max_scene_failures=1)),
            journal,
            stops.append,
        )
        adapter.handle_event(
            make_event(2),
            snapshot_id="frame-1",
            action_generation=0,
            now_monotonic=0.0,
            stop_requested=False,
        )
        self.assertIsNone(adapter.handle_poll(now_monotonic=1.0))
        plan = adapter.handle_poll(now_monotonic=3.0)
        self.assertEqual(plan.action, RecoveryAction.REQUEST_STOP)
        self.assertEqual(len(stops), 1)

    def test_request_stop_reaches_game_session_and_no_step_runs_afterwards(self):
        executed = []

        def step_handler(runtime, command):
            executed.append(command)
            return SessionStep()

        class Runtime:
            def close(self):
                pass

        session = GameSessionController(lambda: Runtime(), step_handler)
        session.start()
        adapter = RecoverySessionAdapter(
            RecoveryController(), self.FakeJournal(), session.stop
        )
        plan = adapter.handle_event(
            make_event(2, severity=SelfCheckSeverity.CRITICAL),
            snapshot_id="frame-1",
            action_generation=session.rearm_token,
            now_monotonic=0.0,
            stop_requested=not session.is_running,
        )
        self.assertEqual(plan.action, RecoveryAction.REQUEST_STOP)
        self.assertEqual(session.status, SessionStatus.STOPPED)
        self.assertTrue(
            session.snapshot.stop_reason.startswith("recovery:critical_self_check")
        )
        self.assertFalse(session.is_running)
        with self.assertRaises(ExecutionCancelled):
            session.ensure_running()
        self.assertEqual(executed, [])


if __name__ == "__main__":
    unittest.main()
