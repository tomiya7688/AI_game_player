"""ゲームに依存しない任意登録の仮想遷移モデル選択。"""

from typing import Generic, Mapping

from ai_game_player.transition_model import StateT, TransitionModel


# {
#   責務: [SimulationProvider: 登録済みゲームだけに任意の仮想遷移能力を提供する]
#   フィールド: [models: 呼出側が明示登録したモデルの写し。既存Decision Providerを変更しない]
# }
class SimulationProvider(Generic[StateT]):
    # {
    #   責務: [__init__: モデル登録の写しを保持する]
    #   処理: [外部辞書の後続変更が登録内容へ影響しないようコピーする]
    #   引数: [models: ゲームIDからモデルへの登録。Noneは全て利用不可]
    #   戻り値: [なし]
    # }
    def __init__(
        self, models: Mapping[str, TransitionModel[StateT]] | None = None
    ) -> None:
        self._models = dict(models or {})

    # {
    #   責務: [get_model: 未知ゲームや未登録時を利用不可として返す]
    #   処理: [登録だけを照合し、暗黙に2048や他モデルへ代替しない]
    #   引数: [game_id: 呼出側が選択したゲームID]
    #   戻り値: [TransitionModelまたはNone: Noneなら従来の判断経路を呼出側が維持する]
    # }
    def get_model(self, game_id: str) -> TransitionModel[StateT] | None:
        return self._models.get(game_id)
