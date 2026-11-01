"""信頼済みSensor Adapterが構成する、Plannerと実画面の接続契約。"""

from dataclasses import dataclass
from typing import Any, Protocol

from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.belief_contract import BeliefContext
from ai_game_player.decision_context import DecisionContext
from ai_game_player.models import ScreenObservation
from ai_game_player.planning_information import PlanningObservationModel


# {
#   責務: [PlannerObservationInput: 観測由来Beliefと実行直前照合用semantic参照を束ねる]
#   フィールド: [belief: 予測ではない入力仮説, registration_id/goal_id: trusted登録/目的,
#     semantic_digest: 同じ実観測から構成したstate identity, observation_model: 任意の校正済み情報channel]
# }
@dataclass(frozen=True, slots=True)
class PlannerObservationInput:
    belief: BeliefSnapshot[Any]
    registration_id: str
    goal_id: str
    semantic_digest: str
    observation_model: PlanningObservationModel | None = None

    # {
    #   責務: [__post_init__: Source参照とtyped Beliefを小さな契約へ固定する]
    #   処理: [raw状態やModel自己申告で登録名を作らず、固定された参照を検査する]
    #   引数: [self: Source入力] 戻り値: [なし] エラー: [ValueError: 型/参照不正]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.belief, BeliefSnapshot) or any(
            not isinstance(value, str) or not value or len(value) > 256
            for value in (self.registration_id, self.goal_id)
        ):
            raise ValueError("planning observation requires trusted typed references")
        if (
            not isinstance(self.semantic_digest, str)
            or len(self.semantic_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.semantic_digest)
        ):
            raise ValueError("planning observation requires a semantic state digest")


# {
#   責務: [PlannerObservationAdapter: ゲーム固有認識を汎用Coreへハードコードせずに接続する]
#   フィールド: [なし: trusted hostがscope/clock/状態型を固定する]
# }
class PlannerObservationAdapter(Protocol):
    # {
    #   責務: [prepare: 同じ実観測からtyped Beliefを作る]
    #   処理: [未来state/DOM/Model予測を観測にしない。未知ゲームはNone、staleは明示withhold]
    #   引数: [context/observation: 同時点の判断と実画面, action_epoch: 操作後再計画の世代,
    #     run_generation: 停止照合世代, now_ns: 同clock origin時刻]
    #   戻り値: [PlannerObservationInputまたはNone: 対応能力無しなら既存Baselineへ戻る]
    # }
    def prepare(
        self,
        context: DecisionContext,
        observation: ScreenObservation,
        action_epoch: int,
        run_generation: int,
        now_ns: int,
    ) -> PlannerObservationInput | None: ...

    # {
    #   責務: [semantic_state: fresh画面のstate identityと手番を副作用なしに再構成する]
    #   処理: [Belief Storeを更新せず、現Recognizerの実観測だけを読む]
    #   引数: [observation: 新取得画面, action_epoch: 現実行境界]
    #   戻り値: [tuple[digest,turn]またはNone: 不明なら入力を許可しない]
    # }
    def semantic_state(
        self, observation: ScreenObservation, action_epoch: int
    ) -> tuple[str, int] | None: ...

    # {
    #   責務: [current_belief: 同じ観測scopeの現行不変Snapshotを副作用なしで照会する]
    #   処理: [外部writer更新/失効を検知し、入力に使用した旧版を再生成しない]
    #   引数: [context: 元判断scope] 戻り値: [BeliefSnapshotまたはNone: 現行集合/利用不可]
    # }
    def current_belief(self, context: BeliefContext) -> BeliefSnapshot[Any] | None: ...
