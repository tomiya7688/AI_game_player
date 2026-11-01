"""再観測をProvider内のcached-frame反復から分離する、有界なSession制御。"""

from collections.abc import Callable
from dataclasses import replace
import time
from typing import Any, cast

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.decision_withheld import DecisionDisposition, DecisionWithheld
from ai_game_player.models import ActionDecision
from ai_game_player.observation_freshness import ObservationFreshness
from ai_game_player.planning_outcome import PlanningOutcome


# {
#   責務: [DecisionOrchestrator: 非操作結果を保ちながら、有限回だけ実Sourceへ再取得を依頼する]
#   フィールド: [pipeline: 通常Verifier/Safety付き実行経路, max_reobservations/timeout_ns: I/O側の独立上限,
#     clock_ns: 同じprocessの期限時計]
# }
class DecisionOrchestrator:
    # {
    #   責務: [__init__: 再観測の回数/総時間を有限設定へ固定する]
    #   処理: [既定は2回/3秒、無限ループ設定を拒否する]
    #   引数: [pipeline: 実判断経路, max_reobservations: 最大追加capture数, timeout_ns: 全判断episodeの上限,
    #     clock_ns: monotonic時計] 戻り値: [なし] エラー: [ValueError: 予算違反]
    # }
    def __init__(
        self,
        pipeline: Any,
        *,
        max_reobservations: int = 2,
        timeout_ns: int = 3_000_000_000,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            type(max_reobservations) is not int
            or not 0 <= max_reobservations <= 8
            or type(timeout_ns) is not int
            or not 1 <= timeout_ns <= 30_000_000_000
        ):
            raise ValueError("decision reobservation budgets must be finite")
        self.pipeline, self.max_reobservations, self.timeout_ns, self.clock_ns = (
            pipeline,
            max_reobservations,
            timeout_ns,
            clock_ns,
        )

    # {
    #   責務: [run: 最初の一判断または有限なfresh再観測後の結果を返す]
    #   処理: [SELECTは既存Pipelineへ任せ、他の語彙を実行recordにしない。再取得失敗は保留へ戻す]
    #   引数: [execute: Trueは選択1手の実行まで、arguments: 既存Pipeline入力]
    #   戻り値: [ActionDecision/ExecutionResult/DecisionDisposition: 操作または入力なしの結果]
    # }
    def run(
        self, *, execute: bool = False, **arguments: Any
    ) -> ActionDecision | ExecutionResult | DecisionDisposition:
        started = self.clock_ns()
        attempts = 0
        while True:
            try:
                call = self.pipeline.run_and_execute if execute else self.pipeline.run
                self.pipeline.reobservation_check = (
                    self._deadline_check(started) if attempts else None
                )
                result = (
                    call(**arguments, require_fresh_observation=True)
                    if attempts
                    else call(**arguments)
                )

                if attempts and not execute:
                    self._deadline_check(started)()
                return cast(ActionDecision | ExecutionResult, result)
            except DecisionWithheld as withheld:
                disposition = replace(withheld.disposition, reobservations=attempts)
                if disposition.outcome != PlanningOutcome.OBSERVE_MORE:
                    return disposition
                if (
                    attempts >= self.max_reobservations
                    or self.clock_ns() - started >= self.timeout_ns
                ):
                    return replace(
                        disposition,
                        outcome=PlanningOutcome.ABSTAIN,
                        reason_codes=(
                            *disposition.reason_codes[:30],
                            "reobservation_budget_exhausted",
                        ),
                    )
                if not callable(getattr(self.pipeline.source, "read_fresh", None)):
                    return replace(
                        disposition,
                        outcome=PlanningOutcome.ABSTAIN,
                        reason_codes=("fresh_source_unavailable",),
                    )
                try:
                    previous = ObservationFreshness.from_observation(
                        self.pipeline.last_observation
                    )
                except (ValueError, AttributeError):
                    return replace(
                        disposition,
                        outcome=PlanningOutcome.ABSTAIN,
                        reason_codes=("capture_reference_unavailable",),
                    )
                self.pipeline.expected_fresh_reference = previous
                attempts += 1
            finally:
                self.pipeline.reobservation_check = None

    # {
    #   責務: [_deadline_check: 有限再観測の総時間を入力直前にも検査する]
    #   処理: [Source取得が期限を過ぎたら、その後のSELECTを実入力へ渡さない]
    #   引数: [started: episode開始時計] 戻り値: [Callable: I/Oなしの期限guard]
    # }
    def _deadline_check(self, started: int) -> Callable[[], None]:
        def check() -> None:
            if self.clock_ns() - started >= self.timeout_ns:
                raise DecisionWithheld(
                    DecisionDisposition(
                        PlanningOutcome.ABSTAIN,
                        ("reobservation_budget_exhausted",),
                        "decision_orchestrator",
                    )
                )

        return check
