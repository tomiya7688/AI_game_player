from dataclasses import dataclass
from pathlib import Path

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
    ) -> None:
        self.dry_run = dry_run
        self.live_executor = live_executor
        self.window_handle = window_handle
        self.input_mode = input_mode
        production_live = not dry_run and live_executor is None
        if safety_guard is None:
            if safety_config is None:
                safety_config = SafetyGuardConfig(
                    require_target=production_live,
                    require_foreground=production_live and input_mode == "mouse",
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
        if production_live:
            ensure_default_emergency_monitor()

    def execute(self, candidate: ActionCandidate) -> ExecutionResult:
        decision = self.safety_guard.check(candidate)
        if not decision.allowed:
            raise RuntimeError(f"SafetyGuard blocked action [{decision.code}]: {decision.reason}")
        if self.dry_run:
            return ExecutionResult(candidate.action_id, False, "dry_run", "OS入力は無効です")
        executor = self.live_executor
        if executor is None:
            from ai_game_player.windows_input import WindowsInputExecutor

            executor = WindowsInputExecutor(
                self.window_handle,
                self.input_mode,
                stop_checker=self.emergency_stop.is_triggered,
            )
            self.live_executor = executor
        return executor.execute(candidate)

    def record_progress(self, state_changed: bool) -> None:
        self.safety_guard.record_progress(state_changed)

    def trigger_emergency_stop(self, reason: str = "emergency stop requested") -> None:
        self.safety_guard.trigger_emergency_stop(reason)
        executor = self.live_executor
        if executor is not None and hasattr(executor, "release_all"):
            executor.release_all()

    def rearm_safety(self) -> None:
        self.safety_guard.rearm()