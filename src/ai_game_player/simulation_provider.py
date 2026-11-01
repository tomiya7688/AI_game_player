"""ゲームに依存しない任意登録の仮想遷移モデル選択。"""

from dataclasses import dataclass
from typing import Generic, cast

from ai_game_player.transition_model import StateT, TransitionModel


# {
#   責務: [SimulationKey: 異なる状態型を同じRegistryで混ぜずに参照する型付きキー]
#   フィールド: [game_id: 明示選択したゲームID, state_type: そのゲームの不変状態型]
# }
@dataclass(frozen=True, slots=True)
class SimulationKey(Generic[StateT]):
    game_id: str
    state_type: type[StateT]


# {
#   責務: [SimulationProvider: 状態型ごとのキーで任意ゲームの仮想遷移能力を登録する]
#   フィールド: [_models: ゲームIDと状態型に対応する登録。型消去はRegistry内部だけに閉じる]
# }
class SimulationProvider:
    # {
    #   責務: [__init__: モデル登録の写しを保持する]
    #   処理: [空の登録を初期化する。既存Decision Providerを変更しない]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def __init__(self) -> None:
        self._models: dict[tuple[str, type], object] = {}

    # {
    #   責務: [register: キーと同じ状態型のモデルを登録する]
    #   処理: [同じRegistryに異種状態を保持し、重複登録は明示的に拒否する]
    #   引数: [key: ゲームと状態型の識別, model: 同じ型を入出力する純粋なモデル]
    #   戻り値: [なし]
    #   エラー: [ValueError: 同じゲームID・状態型が既に登録済み]
    # }
    def register(
        self, key: SimulationKey[StateT], model: TransitionModel[StateT]
    ) -> None:
        identity = (key.game_id, key.state_type)
        if identity in self._models:
            raise ValueError("simulation model is already registered")
        self._models[identity] = model

    # {
    #   責務: [get_model: 未知ゲームや未登録時を利用不可として返す]
    #   処理: [登録だけを照合し、暗黙に2048や他モデルへ代替しない]
    #   引数: [key: 呼出側が選択したゲームIDと状態型。別状態型を暗黙に返さない]
    #   戻り値: [TransitionModelまたはNone: Noneなら従来の判断経路を呼出側が維持する]
    # }
    def get_model(self, key: SimulationKey[StateT]) -> TransitionModel[StateT] | None:
        # registerと同じ型付きキーの組だけを返す。呼出側にAnyやcastを要求しない。
        return cast(
            TransitionModel[StateT] | None,
            self._models.get((key.game_id, key.state_type)),
        )
