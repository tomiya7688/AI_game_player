from dataclasses import dataclass
from ai_game_player.models import ActionCandidate

@dataclass(frozen=True)
class ExecutionResult:
    action_id: str
    executed: bool
    mode: str
    detail: str

class ActionExecutor:
    def __init__(self, dry_run: bool = True) -> None: self.dry_run=dry_run
    def execute(self, candidate: ActionCandidate) -> ExecutionResult:
        if self.dry_run: return ExecutionResult(candidate.action_id,False,"dry_run","OS入力は無効です")
        raise RuntimeError("実入力Executorは明示的な実装が必要です")