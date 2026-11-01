"""実操作や偽候補へ変換しない、共通の判断保留・再観測結果。"""

from dataclasses import dataclass
from typing import Any

from ai_game_player.planning_outcome import PlanningOutcome


# {
#   責務: [DecisionDisposition: 入力を提案しなかった判断をSessionへ伝える]
#   フィールド: [outcome: 共通の非SELECT語彙, reason_codes: 有界な固定理由,
#     provider_id: 判断元, context_snapshot_id: 判断材料の参照, reobservations: 実際の再取得回数]
# }
@dataclass(frozen=True, slots=True)
class DecisionDisposition:
    outcome: PlanningOutcome
    reason_codes: tuple[str, ...]
    provider_id: str
    context_snapshot_id: str | None = None
    reobservations: int = 0

    # {
    #   責務: [__post_init__: 非操作結果だけを有限構造で受け入れる]
    #   処理: [SELECT、空理由、不正な回数と過大文字列を拒否する]
    #   引数: [self: 保留結果] 戻り値: [なし] エラー: [ValueError: 型/容量違反]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self.outcome, PlanningOutcome)
            or self.outcome == PlanningOutcome.SELECT
        ):
            raise ValueError("a nonselect disposition cannot contain an action")
        if (
            type(self.reason_codes) is not tuple
            or not 1 <= len(self.reason_codes) <= 32
            or any(
                not isinstance(reason, str) or not reason or len(reason) > 256
                for reason in self.reason_codes
            )
        ):
            raise ValueError("decision disposition requires bounded reason codes")
        if (
            not isinstance(self.provider_id, str)
            or not self.provider_id
            or len(self.provider_id) > 128
        ):
            raise ValueError("decision disposition requires a provider reference")
        if self.context_snapshot_id is not None and (
            not isinstance(self.context_snapshot_id, str)
            or not self.context_snapshot_id
            or len(self.context_snapshot_id) > 512
        ):
            raise ValueError("invalid withheld context reference")
        if type(self.reobservations) is not int or not 0 <= self.reobservations <= 8:
            raise ValueError("invalid reobservation count")

    # {
    #   責務: [to_dict: 操作IDや実行成功を含めない監査表現へ変換する]
    #   処理: [固定語彙と参照だけを返す] 引数: [なし] 戻り値: [dict: 入力なしの判断結果]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason_codes": list(self.reason_codes),
            "provider_id": self.provider_id,
            "context_snapshot_id": self.context_snapshot_id,
            "reobservations": self.reobservations,
        }


# {
#   責務: [DecisionWithheld: 下位Pipelineを操作前に脱出し、上位orchestratorへ非操作結果を渡す]
#   フィールド: [disposition: 偽ActionIDを持たない構造化結果]
# }
class DecisionWithheld(ValueError):
    # {
    #   責務: [__init__: 型付き非操作結果を既存ValueError互換で送出する]
    #   処理: [理由codeだけを例外文にする] 引数: [disposition: 非操作結果]
    #   戻り値: [なし]
    # }
    def __init__(self, disposition: DecisionDisposition) -> None:
        self.disposition = disposition
        super().__init__(
            f"decision withheld: {disposition.outcome.value} ({', '.join(disposition.reason_codes)})"
        )
