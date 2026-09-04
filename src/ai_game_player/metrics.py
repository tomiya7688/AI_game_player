from collections import Counter
from dataclasses import dataclass
from typing import Iterable
from ai_game_player.action_executor import ExecutionResult

@dataclass(frozen=True)
class ExecutionMetrics:
    total: int
    dry_run: int
    executed: int
    failed: int
    action_counts: dict[str,int]

class MetricsCalculator:
    def calculate(self, results: Iterable[ExecutionResult]) -> ExecutionMetrics:
        items=list(results); counts=Counter(item.action_id for item in items); executed=sum(item.executed for item in items); dry=sum(item.mode=="dry_run" for item in items); failed=sum(item.mode=="failed" for item in items)
        return ExecutionMetrics(len(items),dry,executed,failed,dict(counts))