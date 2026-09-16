from dataclasses import dataclass
from pathlib import Path

from ai_game_player.fail_safe_runtime import FailSafeCommand, FailSafeConfig, FailSafeRuntime, FailSafeState
from ai_game_player.models import ActionCandidate
from ai_game_player.safety_guard import (
    EmergencyStop,
    SafetyGuard,
    SafetyGuardConfig,
    SafetyLog,
    default_emergency_stop,
    ensure_default_emergency_monitor,
)


@dataclass(frozen=True)
class ExecutionResult:
    action_id: str
    executed: bool
    mode: str
    detail: str


class ActionExecutor:
    def __init__(
        self,
        dry_run: bool = True,
        live_executor=None,
        window_handle: int | None = None,
        input_mode: str = "mouse",
        *,
        safety_guard: SafetyGuard | None = None,
        safety_config: SafetyGuardConfig | None = None,
        safety_log_path: Path | None = None,
        emergency_stop: EmergencyStop | None = None,
        fail_safe_runtime: FailSafeRuntime | None = None,
        fail_safe_config: FailSafeConfig | None = None,
        fail_safe_state_directory: Path | None = None,
        external_watchdog: bool = True,
    ) -> None:
        self.dry_run = dry_run
        self.live_executor = live_executor
        self.window_handle = window_handle
        self.input_mode = input_mode
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

    def _stop_requested(self) -> bool:
        runtime = self.fail_safe_runtime
        return self.emergency_stop.is_triggered() or (
            runtime is not None and runtime.state != FailSafeState.ACTIVE
        )

    def _current_target(self):
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
