"""任意登録する2048参照Adapter。汎用Expectimaxへ盤面規則を埋め込まない。"""

import json
import math

from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.game_2048_state import Game2048State
from ai_game_player.game_2048_transition import (
    ACTION_DIRECTION_BY_ID,
    ReferenceGame2048TransitionModel,
)
from ai_game_player.planning_contract import PlanningDomain, PlanningRegistration


# {
#   責務: [Reference2048Quality: 観測済みと仮定した仮想盤面へ明示heuristicを適用する]
#   フィールド: [なし: 空セル/単調性/滑らかさ/最大tile/角配置の既知特徴を使うreference profile。成功確率ではない]
# }
class Reference2048Quality:
    # {
    #   責務: [evaluate: 参照ゲーム内だけの明示特徴を正規化し仮想盤面のqualityを返す]
    #   引数: [state] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def evaluate(self, state: Game2048State) -> NumericQuality:
        if state.over:
            value = -1.0
        elif state.won and not state.keep_playing:
            value = 1.0
        else:
            empty = sum(tile == 0 for row in state.cells for tile in row) / 16
            maximum = max(tile for row in state.cells for tile in row)
            progress = min(1.0, math.log2(maximum) / 11) if maximum else 0.0
            logs = tuple(
                tuple(math.log2(tile) if tile else 0.0 for tile in row)
                for row in state.cells
            )
            lines = list(logs) + [
                tuple(logs[row][column] for row in range(4)) for column in range(4)
            ]
            monotone_penalty = sum(
                min(
                    sum(max(0, line[index + 1] - line[index]) for index in range(3)),
                    sum(max(0, line[index] - line[index + 1]) for index in range(3)),
                )
                for line in lines
            )
            smooth_penalty = sum(
                min(1.0, abs(line[index + 1] - line[index]) / 11)
                for line in lines
                for index in range(3)
                if line[index] and line[index + 1]
            )
            monotonicity = max(0.0, 1.0 - monotone_penalty / 88)
            smoothness = max(0.0, 1.0 - smooth_penalty / 24)
            corners = (
                state.cells[0][0],
                state.cells[0][3],
                state.cells[3][0],
                state.cells[3][3],
            )
            corner = 1.0 if maximum and maximum in corners else -1.0
            # Reference weights are explicit, versioned heuristics; they are not success probabilities.
            value = (
                0.4 * (2 * empty - 1)
                + 0.2 * (2 * monotonicity - 1)
                + 0.1 * (2 * smoothness - 1)
                + 0.15 * (2 * progress - 1)
                + 0.15 * corner
            )
        return NumericQuality(
            value=value,
            confidence=1.0,
            available=True,
            profile_version="reference_2048.v1",
            provenance=("heuristic.reference_2048.v1",),
        )


# {
#   責務: [decode_reference_2048: 既知state schemaのcanonical型付き値だけを復号する]
#   処理: [任意型名をimportせず、fieldとrecord識別を確認して既存不変Game2048Stateへ渡す]
#   引数: [raw: worker内の有界canonical JSON] 戻り値: [Game2048State: 仮想状態]
#   エラー: [ValueError/TypeError/KeyError: schema/型/盤面違反]
# }
def decode_reference_2048(raw: bytes) -> Game2048State:
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"record", "fields"}
        or value["record"] != "ai_game_player.game_2048_state.Game2048State"
        or not isinstance(value["fields"], list)
        or len(value["fields"]) != 5
    ):
        raise ValueError("reference 2048 state schema mismatch")
    members = dict(value["fields"])
    if set(members) != {"cells", "score", "won", "over", "keep_playing"}:
        raise ValueError("reference 2048 fields mismatch")
    cells = tuple(
        tuple(_integer(cell) for cell in row["tuple"])
        for row in members["cells"]["tuple"]
    )
    score = None if members["score"] is None else _integer(members["score"])
    return Game2048State(
        cells, score, members["won"], members["over"], members["keep_playing"]
    )


# {
#   責務: [_integer: 既知canonical整数の桁数とfieldを検証して復号する]
#   引数: [value] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def _integer(value: object) -> int:
    if (
        not isinstance(value, dict)
        or set(value) != {"int"}
        or not isinstance(value["int"], str)
        or len(value["int"]) > 79
    ):
        raise ValueError("reference 2048 integer encoding mismatch")
    return int(value["int"])


# {
#   責務: [create_reference_2048_domain: 既存のpure exact modelへ任意quality/codecを接続する]
#   処理: [合法な抽象操作だけを返し、DOM・capture・実入力へ接続しない]
#   引数: [なし] 戻り値: [PlanningDomain: 登録factoryから制限worker内で作る参照Adapter]
# }
def create_reference_2048_domain() -> PlanningDomain[Game2048State]:
    model = ReferenceGame2048TransitionModel()

    # {
    #   責務: [legal: 参照modelで実際に盤面を変える方向だけを合法とする]
    #   引数: [state] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def legal(state: Game2048State) -> tuple[str, ...]:
        return tuple(
            action
            for action in ACTION_DIRECTION_BY_ID
            if model.apply_action(state, action).changed
        )

    return PlanningDomain(
        Game2048State, decode_reference_2048, model, legal, Reference2048Quality()
    )


# {
#   責務: [reference_2048_registration: trusted起動設定が選べる参照factoryの版を返す]
#   処理: [画面やModel応答をimport先にせず、既知module/factoryだけを宣言する]
#   引数: [なし] 戻り値: [PlanningRegistration: 任意登録用metadata]
# }
def reference_2048_registration() -> PlanningRegistration:
    return PlanningRegistration(
        "reference_2048.v1",
        "ai_game_player.planning_2048",
        "create_reference_2048_domain",
        "2048",
        "2048-state.v1",
        "reference_2048",
        "1",
        "reference_2048_quality",
        "1",
        quality_profile_version="reference_2048.v1",
    )
