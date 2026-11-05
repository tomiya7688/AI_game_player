"""planner worker起動の準備完了を、通常のdecision予算を変えずに待つゲート。"""

from collections.abc import Callable
from math import isfinite
import time
from typing import Any

from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.planning_contract import SearchBudget

MAX_REPORTED_PROBES = 16


# {
#   責務: [PlannerNotReady: 起動待ち期限内にworker往復が通常予算へ収まらなかったことを示す]
#   フィールド: [diagnostic: 有界な固定語彙のprobe記録。入力・状態・frameを含まない]
# }
class PlannerNotReady(RuntimeError):
    def __init__(self, message: str, diagnostic: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


# {
#   責務: [wait_for_planner_ready: 隔離plannerが通常budgetのまま連続して深さ1以上を受領するまで待つ]
#   処理: [budget/cutoffは変更しない。probeは合成beliefで選択・入力・Safetyへ接続せず、
#     失敗probeは連続回数を0へ戻す。残り時間が1 probeの上限未満なら新規probeを始めない]
#   引数: [planner: 実Pipelineと共有しないBoundedExpectimaxPlanner相当, belief_factory: probeごとに新しい合成belief,
#     allowed_action_ids: 合成beliefの合法操作, registration_id/budget: 製品と同じ登録・予算,
#     deadline_monotonic: 起動待ちの絶対期限, required_consecutive: 必要な連続成功回数,
#     retry_interval_s: probe間隔, monotonic/sleep: 時計(テスト用に差替可)]
#   戻り値: [dict: status=ready と有界probe記録]
#   エラー: [ValueError: 引数範囲外, PlannerNotReady: 期限切れ]
# }
def wait_for_planner_ready(
    planner: Any,
    belief_factory: Callable[[], BeliefSnapshot[Any]],
    allowed_action_ids: tuple[str, ...],
    *,
    registration_id: str,
    budget: SearchBudget,
    deadline_monotonic: float,
    required_consecutive: int = 2,
    retry_interval_s: float = 0.25,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if (
        type(required_consecutive) is not int
        or not 1 <= required_consecutive <= 5
        or type(retry_interval_s) not in (int, float)
        or not 0.05 <= retry_interval_s <= 2.0
        or type(deadline_monotonic) not in (int, float)
        or not isfinite(deadline_monotonic)
        or not isinstance(budget, SearchBudget)
    ):
        raise ValueError("planner readiness requires bounded explicit settings")
    started = monotonic()
    probe_seconds = budget.wall_time_ms / 1000
    probes: list[dict[str, Any]] = []
    count = consecutive = 0

    # {
    #   責務: [report: 有界な診断dictを作る]
    #   引数: [status] 戻り値: [dict]
    # }
    def report(status: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": status,
            "probe_count": count,
            "consecutive": consecutive,
            "required_consecutive": required_consecutive,
            "waited_ms": round((monotonic() - started) * 1000, 3),
            "wall_time_ms": budget.wall_time_ms,
            "probes": probes[-MAX_REPORTED_PROBES:],
        }

    while True:
        if deadline_monotonic - monotonic() < probe_seconds:
            diagnostic = report("not_ready")
            last = probes[-1]["reason"] if probes else "no_probe"
            raise PlannerNotReady(
                "planner worker not ready within the startup deadline "
                f"(probes={count}, last_reason={last})",
                diagnostic,
            )
        probe_started = monotonic()
        result = planner.plan(
            belief_factory(),
            allowed_action_ids,
            registration_id=registration_id,
            budget=budget,
        )
        ready = (
            result.completed_depth >= 1 and result.limit_enforcement != "not_applied"
        )
        count += 1
        consecutive = consecutive + 1 if ready else 0
        probes.append(
            {
                "index": count - 1,
                "ready": ready,
                "reason": str(result.reason)[:128],
                "completed_depth": result.completed_depth,
                "elapsed_ms": round((monotonic() - probe_started) * 1000, 3),
            }
        )
        if consecutive >= required_consecutive:
            return report("ready")
        sleep(retry_interval_s)
