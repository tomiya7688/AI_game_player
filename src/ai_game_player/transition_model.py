"""ゲームに依存しない、入力を実行しない遷移モデルの契約。"""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar


StateT = TypeVar("StateT")


# {
#   責務: [ActionTransition: 確定操作の結果を確率遷移前の値として表す]
#   フィールド: [state: 操作直後で確率事象前の予測状態,
#     changed: 操作の確定部分で状態が変化したか。確率事象の発生条件はモデルが定義する,
#     reward_delta: この操作の報酬増分。意味と単位は登録モデルが定義する]
# }
@dataclass(frozen=True, slots=True)
class ActionTransition(Generic[StateT]):
    state: StateT
    changed: bool
    reward_delta: float


# {
#   責務: [ChanceOutcome: 確率事象後の予測状態と条件付き確率を対応付ける]
#   フィールド: [state: 実観測と区別して扱う予測状態,
#     probability: 与えた確定遷移に対するこの状態の確率。全分岐の和は1]
# }
@dataclass(frozen=True, slots=True)
class ChanceOutcome(Generic[StateT]):
    state: StateT
    probability: float


# {
#   責務: [TransitionModel: 確定操作と確率遷移を分離した任意ゲームの仮想モデル契約]
#   フィールド: [なし: 実装の保持状態やOS入力手段を契約へ要求しない]
# }
class TransitionModel(Protocol[StateT]):
    # {
    #   責務: [apply_action: 許可された仮想操作の確定部分だけを評価する]
    #   処理: [実入力を発行せず、モデルに渡された状態だけを遷移させる]
    #   引数: [state: 呼出側の状態仮説, action_id: 呼出側が許可したゲーム操作ID]
    #   戻り値: [ActionTransition: chance展開前の予測]
    # }
    def apply_action(
        self, state: StateT, action_id: str
    ) -> ActionTransition[StateT]: ...

    # {
    #   責務: [chance_outcomes: 確定操作後の確率分布を列挙する]
    #   処理: [モデル固有の確率事象を列挙する。確率事象が無い場合も確率1の状態を返す]
    #   引数: [after_action: 同じモデルで生成した確定遷移]
    #   戻り値: [tuple: 合計確率1の次状態。実観測ではない]
    # }
    def chance_outcomes(
        self, after_action: ActionTransition[StateT]
    ) -> tuple[ChanceOutcome[StateT], ...]: ...
