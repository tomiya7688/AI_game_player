"""信頼済み構成から注入する、ゲーム固有コードを含まない6軸重み方針。"""

from dataclasses import dataclass
from math import isfinite
from typing import Any

from ai_game_player.evaluation_primitives import DEFAULT_WEIGHTS, PRIMITIVES
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier


# {
#   責務: [EvaluationProfile: 価値基準とその適用範囲をimmutableに保持する]
#   フィールド: [profile_id/version: 方針識別子, weights: 6軸の非負重み,
#     scope/goal_id: 任意の厳密一致制約。Noneは汎用方針, source_id/source_version: 信頼済み構成の出所]
# }
@dataclass(frozen=True, slots=True)
class EvaluationProfile:
    profile_id: str = "generic.default"
    version: str = "1"
    weights: tuple[tuple[str, float], ...] = tuple(DEFAULT_WEIGHTS.items())
    scope: KnowledgeScope | None = None
    goal_id: str | None = None
    source_id: str = "builtin.generic_weights"
    source_version: str = "1"

    # {
    #   責務: [__post_init__: 無限weight、未知軸、自由文参照、mutable設定を拒否する]
    #   処理: [全6軸・順序・上限1000とscope型を検証する]
    #   引数: [self: 構築する方針] 戻り値: [なし] エラー: [ValueError: 契約違反]
    # }
    def __post_init__(self) -> None:
        for name in ("profile_id", "version", "source_id", "source_version"):
            validate_identifier(getattr(self, name), "evaluation profile reference")
        if self.goal_id is not None:
            validate_identifier(self.goal_id, "evaluation goal")
        if self.scope is not None and not isinstance(self.scope, KnowledgeScope):
            raise ValueError("evaluation profile requires a typed scope")
        if (
            type(self.weights) is not tuple
            or len(self.weights) != len(PRIMITIVES)
            or any(type(item) is not tuple or len(item) != 2 for item in self.weights)
            or tuple(item[0] for item in self.weights) != PRIMITIVES
            or any(
                type(value) not in (int, float)
                or not 0 <= value <= 1000
                or not isfinite(value)
                for _, value in self.weights
            )
        ):
            raise ValueError("evaluation profile requires six bounded ordered weights")

    # {
    #   責務: [matches: hostが選択したscope/goalへ方針を適用できるか照合する]
    #   処理: [OCRやModelのscene自己申告を使わず、明示制約だけを厳密一致で照合する]
    #   引数: [scope: 信頼済みSession scope, goal_id: 信頼済み目標参照]
    #   戻り値: [bool: 汎用または一致する方針か]
    # }
    def matches(self, scope: KnowledgeScope | None, goal_id: str) -> bool:
        return (self.scope is None or self.scope == scope) and (
            self.goal_id is None or self.goal_id == goal_id
        )

    # {
    #   責務: [to_dict: 適用weightと設定のprovenanceを独立JSON値へ変換する]
    #   処理: [immutable参照と6軸weightをコピーする]
    #   引数: [なし] 戻り値: [dict: local監査用方針]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "weights": dict(self.weights),
            "scope": self.scope.to_dict() if self.scope else None,
            "goal_id": self.goal_id,
            "source_id": self.source_id,
            "source_version": self.source_version,
        }

    # {
    #   責務: [from_dict: 監査projectionの方針を同じ型境界で再検証する]
    #   処理: [6軸weightと既知fieldだけを受理し、scope/参照をconstructorへ渡す]
    #   引数: [value: local JSON方針] 戻り値: [EvaluationProfile: 検証済み方針]
    #   エラー: [ValueError: 形式/参照/数値不正]
    # }
    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvaluationProfile":
        if (
            set(value)
            != {
                "profile_id",
                "version",
                "weights",
                "scope",
                "goal_id",
                "source_id",
                "source_version",
            }
            or type(value["weights"]) is not dict
            or set(value["weights"]) != set(PRIMITIVES)
        ):
            raise ValueError("evaluation profile fields do not match contract")
        scope = (
            KnowledgeScope.from_dict(value["scope"])
            if value["scope"] is not None
            else None
        )
        return cls(
            value["profile_id"],
            value["version"],
            tuple((axis, value["weights"][axis]) for axis in PRIMITIVES),
            scope,
            value["goal_id"],
            value["source_id"],
            value["source_version"],
        )
