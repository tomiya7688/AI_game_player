import unittest
from collections.abc import Callable

from ai_game_player.game_session import (
    GameSessionController,
    LoopObservation,
    SessionStatus,
    SessionStep,
)
from ai_game_player.models import ScreenObservation


class FakeRuntime:
    def __init__(self, name: int, close_error: Exception | None = None) -> None:
        self.name = name
        self.close_count = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_count += 1
        if self.close_error:
            raise self.close_error


class FakeScheduler:
    def __init__(self) -> None:
        self.next_id = 0
        self.pending: dict[int, Callable[[], None]] = {}
        self.delays: list[int] = []
        self.cancelled: set[int] = set()

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> object:
        self.next_id += 1
        self.pending[self.next_id] = callback
        self.delays.append(delay_ms)
        return self.next_id

    def cancel(self, token: object) -> None:
        self.cancelled.add(int(token))

    def run_next(self) -> None:
        token = next(token for token in self.pending if token not in self.cancelled)
        self.pending.pop(token)()

    def callback(self, token: int) -> Callable[[], None]:
        return self.pending[token]


class GameSessionControllerTests(unittest.TestCase):
    def make_controller(self, step_handler=None, loop_observer=None, scheduler=None, on_error=None, on_state_change=None):
        runtimes = []

        def create_runtime():
            runtime = FakeRuntime(len(runtimes) + 1)
            runtimes.append(runtime)
            return runtime

        calls = []
        if step_handler is None:
            def step_handler(runtime, command):
                calls.append((runtime.name, command))
                return SessionStep()

        controller = GameSessionController(
            create_runtime,
            step_handler,
            scheduler=scheduler,
            loop_observer=loop_observer,
            on_state_change=on_state_change,
            on_error=on_error,
        )
        return controller, runtimes, calls

    def test_pipeline_runtime_is_shared_across_steps_and_closed_once_on_stop(self):
        controller, runtimes, calls = self.make_controller()
        controller.step("decide")
        controller.step("execute")

        self.assertEqual(len(runtimes), 1)
        self.assertEqual(calls, [(1, "decide"), (1, "execute")])
        self.assertEqual(controller.snapshot.steps, 2)
        self.assertTrue(controller.run_control.is_running)

        controller.stop()
        self.assertEqual(runtimes[0].close_count, 1)
        self.assertEqual(controller.status, SessionStatus.STOPPED)
        self.assertFalse(controller.run_control.is_running)
        controller.stop()
        self.assertEqual(runtimes[0].close_count, 1)

    def test_restart_creates_a_fresh_runtime_after_stop(self):
        controller, runtimes, calls = self.make_controller()
        controller.start()
        controller.step()
        controller.stop()
        controller.step()

        self.assertEqual(len(runtimes), 2)
        self.assertEqual([runtime.close_count for runtime in runtimes], [1, 0])
        self.assertEqual(calls, [(1, "execute"), (2, "execute")])
        self.assertEqual(controller.rearm_token, 2)

    def test_continuous_loop_schedules_and_reuses_the_session_runtime(self):
        scheduler = FakeScheduler()
        controller, runtimes, calls = self.make_controller(
            scheduler=scheduler,
            loop_observer=lambda: LoopObservation(ScreenObservation("menu", 1, 1)),
        )
        controller.start_loop(command="execute", interval_ms=25)
        scheduler.run_next()
        scheduler.run_next()

        self.assertEqual(scheduler.delays, [25, 25, 25])
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(calls, [(1, "execute"), (1, "execute")])
        self.assertTrue(controller.is_looping)

    def test_repeat_and_terminal_outcomes_stop_before_another_action(self):
        scheduler = FakeScheduler()
        controller, runtimes, calls = self.make_controller(
            scheduler=scheduler,
            loop_observer=lambda: LoopObservation(ScreenObservation("menu", 1, 1)),
        )
        controller.start_loop(interval_ms=1)
        scheduler.run_next()
        scheduler.run_next()
        scheduler.run_next()

        self.assertEqual(calls, [(1, "execute"), (1, "execute")])
        self.assertEqual(controller.status, SessionStatus.COMPLETED)
        self.assertEqual(controller.snapshot.stop_reason, "repeated observation")
        self.assertEqual(runtimes[0].close_count, 1)
        self.assertFalse(controller.is_looping)

        terminal_scheduler = FakeScheduler()
        terminal, terminal_runtimes, terminal_calls = self.make_controller(
            scheduler=terminal_scheduler,
            loop_observer=lambda: LoopObservation(ScreenObservation("victory", 1, 1), "success"),
        )
        terminal.start_loop(interval_ms=1)
        terminal_scheduler.run_next()
        self.assertEqual(terminal_calls, [])
        self.assertEqual(terminal.snapshot.stop_reason, "outcome: success")
        self.assertEqual(terminal_runtimes[0].close_count, 1)

    def test_capture_failure_ends_the_loop_and_cleans_up(self):
        scheduler = FakeScheduler()
        controller, runtimes, calls = self.make_controller(
            scheduler=scheduler,
            loop_observer=lambda: LoopObservation(None, terminal_reason="capture failure"),
        )
        controller.start_loop(interval_ms=1)
        scheduler.run_next()
        self.assertEqual(calls, [])
        self.assertEqual(controller.snapshot.stop_reason, "capture failure")
        self.assertEqual(runtimes[0].close_count, 1)

    def test_loop_observer_failure_fails_and_cleans_up(self):
        errors = []
        scheduler = FakeScheduler()

        def fail_observation():
            raise ValueError("bad observation")

        controller, runtimes, calls = self.make_controller(
            scheduler=scheduler,
            loop_observer=fail_observation,
            on_error=errors.append,
        )
        controller.start_loop(interval_ms=1)
        scheduler.run_next()

        self.assertEqual(controller.status, SessionStatus.FAILED)
        self.assertFalse(controller.is_looping)
        self.assertEqual(controller.snapshot.error, "bad observation")
        self.assertEqual(str(errors[-1]), "bad observation")
        self.assertEqual(runtimes[0].close_count, 1)
        self.assertEqual(calls, [])

    def test_state_notification_failure_does_not_leave_loop_start_ambiguous(self):
        errors = []
        scheduler = FakeScheduler()
        controller, runtimes, calls = self.make_controller(
            scheduler=scheduler,
            loop_observer=lambda: LoopObservation(ScreenObservation("menu", 1, 1)),
            on_state_change=lambda _snapshot: (_ for _ in ()).throw(RuntimeError("status UI failed")),
            on_error=errors.append,
        )

        controller.start_loop(interval_ms=1)
        self.assertTrue(controller.is_looping)
        scheduler.run_next()

        self.assertTrue(controller.is_looping)
        self.assertEqual(controller.status, SessionStatus.RUNNING)
        self.assertEqual(calls, [(1, "execute")])
        self.assertEqual(runtimes[0].close_count, 0)
        self.assertTrue(errors)
        self.assertTrue(all(str(error) == "status UI failed" for error in errors))

    def test_stop_cancels_pending_timer_and_ignores_stale_callback(self):
        scheduler = FakeScheduler()
        controller, runtimes, calls = self.make_controller(
            scheduler=scheduler,
            loop_observer=lambda: LoopObservation(ScreenObservation("menu", 1, 1)),
        )
        controller.start_loop(interval_ms=1)
        stale = scheduler.callback(1)
        controller.stop("manual stop")
        stale()

        self.assertIn(1, scheduler.cancelled)
        self.assertEqual(calls, [])
        self.assertEqual(controller.snapshot.stop_reason, "manual stop")
        self.assertEqual(runtimes[0].close_count, 1)

    def test_step_error_transitions_to_failed_and_releases_runtime(self):
        errors = []
        def fail(_runtime, _command):
            raise ValueError("bad input")

        controller, runtimes, _calls = self.make_controller(step_handler=fail, on_error=errors.append)
        with self.assertRaisesRegex(ValueError, "bad input"):
            controller.step()

        self.assertEqual(controller.status, SessionStatus.FAILED)
        self.assertEqual(controller.snapshot.error, "bad input")
        self.assertEqual(str(errors[0]), "bad input")
        self.assertEqual(runtimes[0].close_count, 1)

    def test_runtime_creation_failure_does_not_leave_run_control_armed(self):
        errors = []
        controller = GameSessionController(
            lambda: (_ for _ in ()).throw(RuntimeError("startup failed")),
            lambda _runtime, _command: SessionStep(),
            on_error=errors.append,
        )
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            controller.start()
        self.assertEqual(controller.status, SessionStatus.FAILED)
        self.assertFalse(controller.run_control.is_running)
        self.assertEqual(len(errors), 1)

    def test_missing_loop_dependencies_and_runtime_close_failure_are_reported(self):
        controller, _runtimes, _calls = self.make_controller()
        with self.assertRaisesRegex(RuntimeError, "scheduler and observation"):
            controller.start_loop()
        with self.assertRaises(ValueError):
            controller.start_loop(interval_ms=0)

        closed_runtime = FakeRuntime(1, RuntimeError("close failed"))
        closing = GameSessionController(lambda: closed_runtime, lambda _r, _c: SessionStep())
        closing.start()
        closing.stop()
        self.assertEqual(closing.status, SessionStatus.FAILED)
        self.assertEqual(closing.snapshot.error, "close failed")
        with self.assertRaisesRegex(RuntimeError, "previous session runtime"):
            closing.start()
        self.assertEqual(closed_runtime.close_count, 1)

        closing.stop()
        self.assertEqual(closing.status, SessionStatus.FAILED)
        self.assertEqual(closed_runtime.close_count, 2)
        closed_runtime.close_error = None
        closing.stop()
        self.assertEqual(closing.status, SessionStatus.STOPPED)
        self.assertEqual(closed_runtime.close_count, 3)


if __name__ == "__main__":
    unittest.main()
