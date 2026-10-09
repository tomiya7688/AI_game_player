"""GUI-independent ownership and scheduling for one game session."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.loop_guard import LoopGuard
from ai_game_player.models import ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.run_control import RunController


# {
#   責務: [SessionRuntime: GameSessionControllerが操作するPipelineとsession-scoped Providerの公開契約]
# }
class SessionRuntime(Protocol):
    # {
    #   責務: [run: session内の判断処理を実行する]
    #   引数: [arguments: Pipelineの判断用入力]
    #   戻り値: [ActionDecision: Providerが選んだ許可候補]
    # }
    def run(self, **arguments: Any) -> ActionDecision: ...

    # {
    #   責務: [run_and_execute: session内の判断と候補実行を行う]
    #   引数: [arguments: Pipelineの実行用入力]
    #   戻り値: [ExecutionResult: 選択候補の実行結果]
    # }
    def run_and_execute(self, **arguments: Any) -> ExecutionResult: ...

    # {
    #   責務: [load_execution_history: 現Session runtimeが管理する互換履歴を読み込む]
    #   引数: [なし]
    #   戻り値: [list[ExecutionResult]: 旧履歴と現在Sessionの実行結果を時系列で返す]
    # }
    def load_execution_history(self) -> list[ExecutionResult]: ...

    # {
    #   責務: [assess_outcome: session-scoped Providerで現在画面のterminal状態を評価する]
    #   引数: [observation: 現在画面, previous: session中の直前画面]
    #   戻り値: [OutcomeAssessment: success/failure/ongoing状態と根拠]
    # }
    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None,
    ) -> OutcomeAssessment: ...

    # {
    #   責務: [close: Session終了時にruntimeが所有するresourceを解放する]
    #   引数: []
    #   戻り値: []
    # }
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
    deferred: bool = False


@dataclass(frozen=True)
class LoopObservation:
    screen: ScreenObservation | None
    outcome_status: str = "ongoing"
    terminal_reason: str | None = None
    deferred: bool = False


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
        self._step_pending = False
        self._observation_pending = False
        self._pending_observation_generation: int | None = None

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

    # {
    #   責務: [has_pending_step: 完了通知を待つstepがあるか返す]
    #   処理: [非同期step handlerがdeferredを返した後、worker結果を受け取るまで保持する状態を返す]
    #   引数: []
    #   戻り値: [bool: complete_stepによる完了通知を待っている場合はTrue]
    # }
    @property
    def has_pending_step(self) -> bool:
        return self._step_pending

    def start(self) -> None:
        if self.is_running:
            return
        if self._runtime is not None:
            raise RuntimeError("The previous session runtime has not been closed")
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
        if self._step_pending:
            raise RuntimeError("A previous session step has not completed")
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
        if result.deferred:
            self._step_pending = True
            self._notify()
            return result
        self._steps += 1
        if result.terminal_reason is not None:
            self._finish(SessionStatus.COMPLETED, result.terminal_reason)
        else:
            self._notify()
        return result

    # {
    #   責務: [complete_step: workerで実行したstepの完了・失敗をSession状態へ反映する]
    #   処理: [停止済みstepはruntimeを解放し、実行中のstepはstep数を更新してloopを再予約する]
    #   引数: [result: workerが返したterminal情報, error: workerで発生した例外]
    #   戻り値: [bool: 対応するpending stepを完了できた場合はTrue]
    # }
    def complete_step(self, result: SessionStep | None = None, error: Exception | None = None) -> bool:
        if not self._step_pending:
            return False
        self._step_pending = False

        if not self.is_running:
            close_error = self._close_runtime()
            if close_error is not None:
                self._mark_shutdown_failed(close_error)
            return True

        if error is not None:
            self._fail(error)
            return True

        completed = result or SessionStep()
        self._steps += 1
        if completed.terminal_reason is not None:
            self._finish(SessionStatus.COMPLETED, completed.terminal_reason)
        else:
            self._notify()
            if self.is_looping:
                self._schedule_next(self._loop_generation)
        return True

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
        self._observation_pending = False
        self._pending_observation_generation = None
        self._status = SessionStatus.STOPPED
        self._stop_reason = reason
        self._error = None
        self._notify()
        close_error = None if self._step_pending else self._close_runtime()
        if close_error is not None:
            self._status = SessionStatus.FAILED
            self._stop_reason = "runtime shutdown failed"
            self._error = str(close_error)
            self._notify_error(close_error)
            self._notify()

    def ensure_running(self) -> None:
        """RunController-compatible safety check used by DecisionPipeline."""
        self.run_control.ensure_running()

    # {
    #   責務: [assess_outcome: Sessionが所有するProviderでloop中の画面状態を評価する]
    #   処理: [active session runtimeへ現在画面と直前画面を渡し、session scoped Providerを再利用する]
    #   引数: [observation: terminal判定する現在画面, previous: 直前の画面観測]
    #   戻り値: [OutcomeAssessment: success/failure/ongoing状態と根拠]
    #   エラー: [RuntimeError: Session runtimeが未作成または評価機能を持たない場合]
    # }
    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None,
    ) -> OutcomeAssessment:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("A session runtime is not active")
        return runtime.assess_outcome(observation, previous)

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

    # {
    #   責務: [_run_loop_step: loop timerから観測・評価・step開始を順に実行する]
    #   処理: [世代を検証し、非同期観測ならpendingとして待ち、同期観測なら判定へ渡す]
    #   引数: [generation: callback予約時のloop世代]
    #   戻り値: []
    # }
    def _run_loop_step(self, generation: int) -> None:
        self._scheduled_token = None
        if generation != self._loop_generation or not self.is_looping:
            return
        try:
            if self._loop_observer is None:
                raise RuntimeError("The continuous observation source is unavailable")
            observation = self._loop_observer()
            if observation.deferred:
                if generation != self._loop_generation or not self.is_looping:
                    return
                self._observation_pending = True
                self._pending_observation_generation = generation
                return
            if not self._apply_loop_observation(observation, generation):
                return
        except Exception as exc:
            if self._status is not SessionStatus.FAILED:
                self._fail(exc)
            return

    # {
    #   責務: [complete_loop_observation: 非同期状態評価結果を連続実行の判定へ反映する]
    #   処理: [停止後・古いloop世代の結果を捨て、有効な観測だけをterminal/repeat検査へ渡す]
    #   引数: [observation: workerで評価した画面状態とterminal結果]
    #   戻り値: [bool: 現在のpending observationを適用した場合はTrue]
    # }
    def complete_loop_observation(self, observation: LoopObservation) -> bool:
        generation = self._pending_observation_generation
        if (
            not self._observation_pending
            or generation is None
            or generation != self._loop_generation
            or not self.is_looping
        ):
            return False
        self._observation_pending = False
        self._pending_observation_generation = None
        return self._apply_loop_observation(observation, generation)

    # {
    #   責務: [_apply_loop_observation: 有効なloop観測から停止または次stepを決定する]
    #   処理: [terminal・capture・反復状態を検査し、継続時だけstepを呼び出す]
    #   引数: [observation: 画面と評価済み状態, generation: 観測開始時のloop世代]
    #   戻り値: [bool: 現在のloopへ観測を適用した場合はTrue]
    # }
    def _apply_loop_observation(self, observation: LoopObservation, generation: int) -> bool:
        if generation != self._loop_generation or not self.is_looping:
            return False
        if observation.terminal_reason is not None:
            self._finish(SessionStatus.COMPLETED, observation.terminal_reason)
            return True
        if observation.screen is None:
            self._finish(SessionStatus.COMPLETED, "capture failed")
            return True
        if self._loop_guard.observe(observation.screen):
            self._finish(SessionStatus.COMPLETED, "repeated observation")
            return True
        if observation.outcome_status in {"success", "failure"}:
            self._finish(SessionStatus.COMPLETED, f"outcome: {observation.outcome_status}")
            return True
        result = self.step(self._loop_command, from_loop=True)
        if not result.deferred and generation == self._loop_generation and self.is_looping:
            self._schedule_next(generation)
        return True

    # {
    #   責務: [_finish: 正常停止・terminal終了時にloopとruntimeを閉じる]
    #   処理: [古いcallbackを無効化し、Journalを持つruntimeが有効な間にterminal状態を記録してからresourceを解放する]
    #   引数: [status: 完了または停止状態, reason: 停止・terminal理由]
    #   戻り値: []
    # }
    def _finish(self, status: SessionStatus, reason: str) -> None:
        self._loop_generation += 1
        self._looping = False
        self._observation_pending = False
        self._pending_observation_generation = None
        self._cancel_scheduled()
        self.run_control.stop()
        self._status = status
        self._stop_reason = reason
        self._error = None
        self._notify()
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
            return
        self._notify()

    # {
    #   責務: [_mark_shutdown_failed: 停止後のruntime解放失敗をFAILED状態へ記録する]
    #   処理: [閉じられなかったruntimeの失敗理由を通知し、session再利用を止める]
    #   引数: [error: runtime.closeで発生した例外]
    #   戻り値: []
    # }
    def _mark_shutdown_failed(self, error: Exception) -> None:
        self._status = SessionStatus.FAILED
        self._stop_reason = "runtime shutdown failed"
        self._error = str(error)
        self._notify_error(error)
        self._notify()

    # {
    #   責務: [_fail: startup・step失敗を記録してruntimeを解放する]
    #   処理: [loop callbackを無効化し、例外とshutdown失敗をFAILED状態へ保存する]
    #   引数: [error: 処理中に発生した例外, runtime_already_closed: runtime解放済みならTrue]
    #   戻り値: []
    # }
    def _fail(self, error: Exception, runtime_already_closed: bool = False) -> None:
        self._loop_generation += 1
        self._looping = False
        self._observation_pending = False
        self._pending_observation_generation = None
        self._cancel_scheduled()
        self.run_control.stop()
        detail = str(error)
        self._status = SessionStatus.FAILED
        self._stop_reason = "step or startup failed"
        self._error = detail
        self._notify_error(error)
        self._notify()
        close_error = None if runtime_already_closed else self._close_runtime()
        if close_error is not None:
            detail += f"; runtime shutdown failed: {close_error}"
            self._error = detail
            self._notify()

    def _close_runtime(self) -> Exception | None:
        runtime = self._runtime
        if runtime is None:
            return None
        try:
            runtime.close()
        except Exception as exc:
            return exc
        self._runtime = None
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
        try:
            self._on_state_change(self.snapshot)
        except Exception as exc:
            self._notify_error(exc)
