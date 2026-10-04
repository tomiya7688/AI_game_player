"""GUI-independent ownership and scheduling for one game session."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ScreenObservation
from ai_game_player.run_control import RunController


class SessionRuntime(Protocol):
    def close(self) -> None: ...


class SessionScheduler(Protocol):
    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> object: ...

    def cancel(self, token: object) -> None: ...


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionStep:
    terminal_reason: str | None = None


@dataclass(frozen=True)
class LoopObservation:
    screen: ScreenObservation | None
    outcome_status: str = "ongoing"
    terminal_reason: str | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    status: SessionStatus
    steps: int
    stop_reason: str | None = None
    error: str | None = None
    looping: bool = False


class GameSessionController:
    """Own one runtime from session start through stop, completion, or failure."""

    def __init__(
        self,
        runtime_factory: Callable[[], SessionRuntime],
        step_handler: Callable[[SessionRuntime, str], SessionStep],
        scheduler: SessionScheduler | None = None,
        loop_observer: Callable[[], LoopObservation] | None = None,
        on_state_change: Callable[[SessionSnapshot], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        run_control: RunController | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._step_handler = step_handler
        self._scheduler = scheduler
        self._loop_observer = loop_observer
        self._loop_guard = LoopGuard()
        self._on_state_change = on_state_change or (lambda _snapshot: None)
        self._on_error = on_error or (lambda _error: None)
        self.run_control = run_control or RunController()
        self.run_control.stop()
        self._runtime: SessionRuntime | None = None
        self._status = SessionStatus.IDLE
        self._steps = 0
        self._stop_reason: str | None = None
        self._error: str | None = None
        self._scheduled_token: object | None = None
        self._looping = False
        self._loop_command = "execute"
        self._loop_interval_ms = 1000
        self._loop_generation = 0

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(self._status, self._steps, self._stop_reason, self._error, self._looping)

    @property
    def is_running(self) -> bool:
        return self._status is SessionStatus.RUNNING and self.run_control.is_running

    @property
    def is_looping(self) -> bool:
        return self._looping and self.is_running

    def start(self) -> None:
        if self.is_running:
            return
        self._loop_generation += 1
        self._looping = False
        self._steps = 0
        self._stop_reason = None
        self._error = None
        self.run_control.start()
        try:
            self._runtime = self._runtime_factory()
        except Exception as exc:
            self.run_control.stop()
            self._fail(exc, runtime_already_closed=True)
            raise
        self._status = SessionStatus.RUNNING
        self._notify()

    def step(self, command: str = "execute", *, from_loop: bool = False) -> SessionStep:
        if self.is_looping and not from_loop:
            raise RuntimeError("A continuous session already owns the step loop")
        if not self.is_running:
            self.start()
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("The session runtime was not created")
        try:
            result = self._step_handler(runtime, command)
        except Exception as exc:
            self._fail(exc)
            raise
        self._steps += 1
        if result.terminal_reason is not None:
            self._finish(SessionStatus.COMPLETED, result.terminal_reason)
        else:
            self._notify()
        return result

    def start_loop(self, command: str = "execute", interval_ms: int = 1000) -> None:
        if interval_ms <= 0:
            raise ValueError("Loop interval must be positive")
        if self._scheduler is None or self._loop_observer is None:
            raise RuntimeError("Continuous sessions require a scheduler and observation source")
        if self.is_looping:
            return
        self.start()
        self._loop_command = command
        self._loop_interval_ms = interval_ms
        self._looping = True
        self._loop_generation += 1
        self._loop_guard.reset()
        self._schedule_next(self._loop_generation, delay_ms=interval_ms)
        self._notify()

    def stop(self, reason: str = "stopped") -> None:
        self._loop_generation += 1
        self._looping = False
        self._cancel_scheduled()
        self.run_control.stop()
        close_error = self._close_runtime()
        if close_error is None:
            self._status = SessionStatus.STOPPED
            self._stop_reason = reason
            self._error = None
        else:
            self._status = SessionStatus.FAILED
            self._stop_reason = "runtime shutdown failed"
            self._error = str(close_error)
            self._notify_error(close_error)
        self._notify()

    def ensure_running(self) -> None:
        """RunController-compatible safety check used by DecisionPipeline."""
        self.run_control.ensure_running()

    @property
    def rearm_token(self) -> int:
        return self.run_control.rearm_token

    def _schedule_next(self, generation: int, delay_ms: int | None = None) -> None:
        if self._scheduler is None or not self.is_looping:
            return
        try:
            self._scheduled_token = self._scheduler.schedule(
                self._loop_interval_ms if delay_ms is None else delay_ms,
                lambda: self._run_loop_step(generation),
            )
        except Exception as exc:
            self._fail(exc)
            raise

    def _run_loop_step(self, generation: int) -> None:
        self._scheduled_token = None
        if generation != self._loop_generation or not self.is_looping:
            return
        try:
            if self._loop_observer is None:
                raise RuntimeError("The continuous observation source is unavailable")
            observation = self._loop_observer()
            if observation.terminal_reason is not None:
                self._finish(SessionStatus.COMPLETED, observation.terminal_reason)
                return
            if observation.screen is None:
                self._finish(SessionStatus.COMPLETED, "capture failed")
                return
            if self._loop_guard.observe(observation.screen):
                self._finish(SessionStatus.COMPLETED, "repeated observation")
                return
            if observation.outcome_status in {"success", "failure"}:
                self._finish(SessionStatus.COMPLETED, f"outcome: {observation.outcome_status}")
                return
            self.step(self._loop_command, from_loop=True)
        except Exception:
            return
        if generation == self._loop_generation and self.is_looping:
            self._schedule_next(generation)

    def _finish(self, status: SessionStatus, reason: str) -> None:
        self._loop_generation += 1
        self._looping = False
        self._cancel_scheduled()
        self.run_control.stop()
        close_error = self._close_runtime()
        if close_error is not None:
            self._status = SessionStatus.FAILED
            self._stop_reason = "runtime shutdown failed"
            self._error = str(close_error)
            self._notify_error(close_error)
        else:
            self._status = status
            self._stop_reason = reason
            self._error = None
        self._notify()

    def _fail(self, error: Exception, runtime_already_closed: bool = False) -> None:
        self._loop_generation += 1
        self._looping = False
        self._cancel_scheduled()
        self.run_control.stop()
        close_error = None if runtime_already_closed else self._close_runtime()
        detail = str(error)
        if close_error is not None:
            detail += f"; runtime shutdown failed: {close_error}"
        self._status = SessionStatus.FAILED
        self._stop_reason = "step or startup failed"
        self._error = detail
        self._notify_error(error)
        self._notify()

    def _close_runtime(self) -> Exception | None:
        runtime = self._runtime
        self._runtime = None
        if runtime is None:
            return None
        try:
            runtime.close()
        except Exception as exc:
            return exc
        return None

    def _cancel_scheduled(self) -> None:
        token = self._scheduled_token
        self._scheduled_token = None
        if token is not None and self._scheduler is not None:
            try:
                self._scheduler.cancel(token)
            except Exception:
                # Stale callbacks are guarded by the generation token.
                pass

    def _notify_error(self, error: Exception) -> None:
        try:
            self._on_error(error)
        except Exception:
            pass

    def _notify(self) -> None:
        self._on_state_change(self.snapshot)
