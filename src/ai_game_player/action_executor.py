from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ai_game_player.fail_safe_runtime import FailSafeCommand, FailSafeConfig, FailSafeRuntime, FailSafeState
from ai_game_player.models import ActionCandidate
from ai_game_player.safety_guard import (
    EmergencyStop,
    SafetyGuard,
    SafetyGuardConfig,
    SafetyLog,
    TargetState,
    default_emergency_stop,
    ensure_default_emergency_monitor,
)


@dataclass(frozen=True)
class ExecutionResult:
    action_id: str
    executed: bool
    mode: str
    detail: str


class LiveExecutor(Protocol):
    def execute(self, candidate: ActionCandidate) -> ExecutionResult: ...


# {
#   責務: [
#     ActionExecutor: 候補の安全評価とOS入力への到達を制御する
#   ]
#   フィールド: [
#     window_handle: 実入力先として選択されたHWND
#     window_process_id: 選択時の対象PID
#     input_mode: OS入力の送信方式
#     dry_run: OS入力を無効にする状態
#   ]
#   処理: [
#     1: SafetyGuardを通す
#     2: 対象が変化していないことを確認する
#     3: 明示されたExecutorだけを呼び出す
#   ]
# }
class ActionExecutor:
    # {
    #   責務: [
    #     __init__: 安全評価・停止制御・対象識別を統合する実行境界を構築する
    #   ]
    #   処理: [
    #     1: dry-runと実入力経路を設定する
    #     2: 対象HWND・PIDを保持する
    #     3: 必要な安全監視とfail-safe runtimeを準備する
    #   ]
    #   引数: [
    #     window_handle: 対象HWND
    #     input_mode: 選択された入力方式
    #     window_process_id: 列挙時に対象HWNDを所有したPID
    #     run_controller_stop_checker: RunControllerが停止済みならTrueを返す確認関数
    #   ]
    #   戻り値: []
    # }
    def __init__(
        self,
        dry_run: bool = True,
        live_executor: LiveExecutor | None = None,
        window_handle: int | None = None,
        input_mode: str = "mouse",
        window_process_id: int | None = None,
        *,
        safety_guard: SafetyGuard | None = None,
        safety_config: SafetyGuardConfig | None = None,
        safety_log_path: Path | None = None,
        emergency_stop: EmergencyStop | None = None,
        fail_safe_runtime: FailSafeRuntime | None = None,
        fail_safe_config: FailSafeConfig | None = None,
        fail_safe_state_directory: Path | None = None,
        external_watchdog: bool = True,
        run_controller_stop_checker: Callable[[], bool] | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.live_executor = live_executor
        self.window_handle = window_handle
        self.input_mode = input_mode
        self.window_process_id = window_process_id
        self.run_controller_stop_checker = run_controller_stop_checker or (lambda: False)
        production_live = not dry_run and live_executor is None
        if safety_guard is None:
            if safety_config is None:
                bound_windows_target = production_live and window_handle is not None
                safety_config = SafetyGuardConfig(
                    require_target=bound_windows_target,
                    require_foreground=bound_windows_target and input_mode == "mouse",
                )
            stop = emergency_stop or (default_emergency_stop() if production_live else EmergencyStop())
            safety_guard = SafetyGuard(
                safety_config,
                window_handle=window_handle,
                emergency_stop=stop,
                log=SafetyLog(safety_log_path),
            )
        self.safety_guard = safety_guard
        self.emergency_stop = safety_guard.emergency_stop

        if fail_safe_runtime is None and production_live and window_handle is not None:
            state_directory = fail_safe_state_directory
            if state_directory is None:
                state_directory = (
                    safety_log_path.parent / "fail_safe"
                    if safety_log_path is not None
                    else Path.cwd() / ".kadoka-fail-safe"
                )
            fail_safe_runtime = FailSafeRuntime(
                state_directory,
                fail_safe_config,
                release_callback=self._release_live_inputs,
                external_watchdog=external_watchdog,
            )
        self.fail_safe_runtime = fail_safe_runtime
        if self.fail_safe_runtime is not None:
            self.fail_safe_runtime.release_callback = self._release_live_inputs
        if production_live:
            ensure_default_emergency_monitor()

    # {
    #   責務: [
    #     execute: 安全評価を通過した候補だけを対象識別が一致する実行先へ渡す
    #   ]
    #   処理: [
    #     1: SafetyGuardで候補を検証する
    #     2: dry-runならOS入力なしで結果を返す
    #     3: 実入力対象のHWNDとPIDを確認する
    #     4: FailSafeRuntimeを確認して選択Executorを呼ぶ
    #   ]
    #   引数: [
    #     candidate: 実行候補
    #     fail_safe_command: 任意のsession command
    #   ]
    #   戻り値: [
    #     ExecutionResult: 実行結果
    #   ]
    #   エラー: [
    #     安全評価・対象識別・Executorが拒否した場合RuntimeError
    #   ]
    # }
    def execute(
        self,
        candidate: ActionCandidate,
        fail_safe_command: FailSafeCommand | None = None,
    ) -> ExecutionResult:
        decision = self.safety_guard.check(candidate)
        if not decision.allowed:
            raise RuntimeError(f"SafetyGuard blocked action [{decision.code}]: {decision.reason}")
        if self.dry_run:
            return ExecutionResult(candidate.action_id, False, "dry_run", "OS入力は無効です")

        if self.live_executor is None and (self.window_handle is None or not self.window_process_id):
            raise RuntimeError("live Windows input requires a selected window and its process identity")

        command = fail_safe_command
        runtime = self.fail_safe_runtime
        if runtime is not None:
            target = self._current_target()
            if target is None or not target.valid or not target.visible or target.pid <= 0:
                runtime.recovery_required("target_invalid_before_execute")
                raise RuntimeError("FailSafeRuntime blocked action [target_invalid]: target is unavailable")
            command = command or runtime.make_command(
                candidate.action_id,
                target_pid=target.pid,
                target_handle=target.handle,
            )
            fail_safe_decision = runtime.check_command(command)
            if not fail_safe_decision.allowed:
                raise RuntimeError(
                    f"FailSafeRuntime blocked action [{fail_safe_decision.code}]: {fail_safe_decision.reason}"
                )

        try:
            executor = self.live_executor
            if executor is None:
                if self.window_handle is None:
                    raise RuntimeError(
                        "SafetyGuard blocked action [target_missing]: live Windows input requires a target window"
                    )
                from ai_game_player.windows_input import WindowsInputExecutor

                executor = WindowsInputExecutor(
                    self.window_handle,
                    self.input_mode,
                    window_process_id=self.window_process_id,
                    stop_checker=self._stop_requested,
                    input_ledger=runtime.ledger if runtime is not None else None,
                    hold_ttl_seconds=runtime.config.hold_ttl_seconds if runtime is not None else None,
                )
                self.live_executor = executor
            return executor.execute(candidate)
        finally:
            if runtime is not None and command is not None:
                runtime.complete_command(command)

    def record_progress(self, state_changed: bool) -> None:
        self.safety_guard.record_progress(state_changed)

    def record_observation(self, observed_at: float | None = None) -> None:
        runtime = self.fail_safe_runtime
        if runtime is None:
            return
        if not runtime.record_observation(observed_at):
            raise RuntimeError(
                f"FailSafeRuntime rejected observation update: state={runtime.state.value}, reason={runtime.reason}"
            )

    def heartbeat(self) -> bool:
        runtime = self.fail_safe_runtime
        return True if runtime is None else runtime.heartbeat()

    def trigger_emergency_stop(self, reason: str = "emergency stop requested") -> None:
        self.safety_guard.trigger_emergency_stop(reason)
        runtime = self.fail_safe_runtime
        if runtime is not None:
            runtime.emergency_stop(reason)
        self._release_live_inputs()

    def rearm_safety(self) -> None:
        self.safety_guard.rearm()
        runtime = self.fail_safe_runtime
        if runtime is None:
            return
        target = self._current_target()
        if target is None or not target.valid or not target.visible or target.pid <= 0:
            runtime.recovery_required("invalid_target_on_rearm")
            raise RuntimeError("FailSafeRuntime re-arm requires a valid visible target")
        runtime.ledger.configure(target_handle=target.handle, input_mode=self.input_mode)
        runtime.rearm(target_pid=target.pid, target_handle=target.handle)

    def close(self) -> None:
        runtime = self.fail_safe_runtime
        if runtime is not None:
            runtime.close()
        self._release_live_inputs()

    @property
    def fail_safe_state(self) -> FailSafeState | None:
        runtime = self.fail_safe_runtime
        return None if runtime is None else runtime.state

    # {
    #   責務: [_stop_requested: UI実行制御・EmergencyStop・FailSafeRuntimeの停止状態を統合する]
    #   処理: [いずれかの停止状態が有効なら、OS入力の継続を禁止する]
    #   引数: []
    #   戻り値: [bool: 実入力を継続できない状態か]
    # }
    def _stop_requested(self) -> bool:
        runtime = self.fail_safe_runtime
        return self.run_controller_stop_checker() or self.emergency_stop.is_triggered() or (
            runtime is not None and runtime.state != FailSafeState.ACTIVE
        )

    def _current_target(self) -> TargetState | None:
        if self.window_handle is None:
            return None
        try:
            return self.safety_guard.target_probe.inspect(self.window_handle)
        except Exception:
            return None

    def _release_live_inputs(self) -> None:
        executor = self.live_executor
        if executor is not None and hasattr(executor, "release_all"):
            try:
                executor.release_all()
            except Exception:
                pass
