from dataclasses import dataclass
from ai_game_player.models import ActionCandidate


@dataclass(frozen=True)
class ExecutionResult:
    action_id: str
    executed: bool
    mode: str
    detail: str


class ActionExecutor:
    def __init__(self, dry_run: bool = True, live_executor=None, window_handle: int | None = None) -> None:
        self.dry_run = dry_run
        self.live_executor = live_executor
        self.window_handle = window_handle

    def execute(self, candidate: ActionCandidate) -> ExecutionResult:
        if self.dry_run:
            return ExecutionResult(candidate.action_id, False, "dry_run", "OS入力は無効です")
        if candidate.kind in {"click", "double_click"} and (candidate.x is None or candidate.y is None):
            raise RuntimeError("click action requires coordinates")
        executor = self.live_executor
        if executor is None:
            from ai_game_player.windows_input import WindowsInputExecutor
            executor = WindowsInputExecutor(self.window_handle)
        return executor.execute(candidate)