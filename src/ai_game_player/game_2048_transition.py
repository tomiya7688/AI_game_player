"""pinned 2048のmax/chance遷移。画面・DOM・入力・global RNGへ依存しない。"""

from dataclasses import replace
import random

from ai_game_player.game_2048_state import BOARD_SIZE, Game2048State
from ai_game_player.transition_model import ChanceOutcome, ActionTransition


DIRECTIONS = ("UP", "RIGHT", "DOWN", "LEFT")
SPAWN_DISTRIBUTION = ((2, 0.9), (4, 0.1))
WINNING_TILE = 2048
ACTION_DIRECTION_BY_ID = {
    f"2048-move-{direction.lower()}": direction for direction in DIRECTIONS
}


# {
#   責務: [ReferenceGame2048TransitionModel: 2048の盤面仮説を純粋な仮想操作と確率spawnで展開する]
#   フィールド: [なし: 入力状態と明示seed以外に実ゲームや乱数の状態を保持しない]
# }
class ReferenceGame2048TransitionModel:
    # {
    #   責務: [apply_action: ゲーム非依存の操作ID契約を2048の確定移動へ接続する]
    #   処理: [登録済みの4操作IDだけを方向へ変換し、仮想盤面を評価する]
    #   引数: [state: 入力された盤面仮説, action_id: 許可候補に由来する2048-move-*のID]
    #   戻り値: [ActionTransition: 確定移動の結果。reward_deltaは合成点を表す]
    #   エラー: [ValueError: モデルが扱わない操作ID]
    # }
    def apply_action(
        self, state: Game2048State, action_id: str
    ) -> ActionTransition[Game2048State]:
        if action_id not in ACTION_DIRECTION_BY_ID:
            raise ValueError(f"unsupported 2048 action: {action_id!r}")
        return self.apply_slide(state, ACTION_DIRECTION_BY_ID[action_id])

    # {
    #   責務: [chance_outcomes: 共通契約から2048の確率spawnを列挙する]
    #   処理: [確定移動結果を同じモデルのspawn分布へ渡す]
    #   引数: [after_action: apply_actionまたはapply_slideの結果]
    #   戻り値: [tuple: 次状態とその条件付き確率]
    #   エラー: [ValueError: changedなのに空セルが無い矛盾した結果]
    # }
    def chance_outcomes(
        self, after_action: ActionTransition[Game2048State]
    ) -> tuple[ChanceOutcome[Game2048State], ...]:
        return self.spawn_outcomes(after_action)

    # {
    #   責務: [apply_slide: 指定方向への移動・一手一合成と得点増分を計算する]
    #   処理: [方向順に各列または行を圧縮・合成する。終了状態とno-opは元状態を返しspawnしない]
    #   引数: [state: 不変の観測仮説または予測状態, direction: UP/RIGHT/DOWN/LEFTのいずれか]
    #   戻り値: [ActionTransition: spawn前盤面、変化の有無、合成点。未認識の累積点はNoneを保つ]
    #   エラー: [ValueError: 未対応の方向名]
    # }
    def apply_slide(
        self, state: Game2048State, direction: str
    ) -> ActionTransition[Game2048State]:
        if direction not in DIRECTIONS:
            raise ValueError(f"unsupported 2048 direction: {direction!r}")
        if state.terminated:
            return ActionTransition(state, False, 0)
        cells = [list(row) for row in state.cells]
        score_delta = 0
        won = state.won
        for line in range(BOARD_SIZE):
            positions = _line_positions(line, direction)
            values = [
                state.cells[row][col] for row, col in positions if state.cells[row][col]
            ]
            merged: list[int] = []
            index = 0
            while index < len(values):
                value = values[index]
                if index + 1 < len(values) and values[index + 1] == value:
                    value *= 2
                    score_delta += value
                    won = won or value == WINNING_TILE
                    index += 1
                merged.append(value)
                index += 1
            merged.extend([0] * (BOARD_SIZE - len(merged)))
            for (row, col), value in zip(positions, merged):
                cells[row][col] = value
        board = tuple(tuple(row) for row in cells)
        if board == state.cells:
            return ActionTransition(state, False, 0)
        score = None if state.score is None else state.score + score_delta
        return ActionTransition(
            replace(state, cells=board, score=score, won=won), True, score_delta
        )

    # {
    #   責務: [spawn_outcomes: 移動後の全空セルへ2/4の条件付き確率を列挙する]
    #   処理: [各空セルは均一、tileは90/10とし、spawn後に合法手が無ければoverを立てる]
    #   引数: [after_slide: apply_slideの結果。勝利した手でも最後のspawnは発生する]
    #   戻り値: [tuple: 最大30分岐。no-opは同一状態のみで乱数生成を行わない]
    #   エラー: [ValueError: movedなのに空セルが無い矛盾した結果]
    # }
    def spawn_outcomes(
        self, after_slide: ActionTransition[Game2048State]
    ) -> tuple[ChanceOutcome[Game2048State], ...]:
        if not after_slide.changed:
            return (ChanceOutcome(after_slide.state, 1.0),)
        empty = _empty_positions(after_slide.state)
        if not empty:
            raise ValueError("a moved 2048 board must have an empty cell before spawn")
        return tuple(
            ChanceOutcome(
                _insert_tile(after_slide.state, row, col, value), mass / len(empty)
            )
            for row, col in empty
            for value, mass in SPAWN_DISTRIBUTION
        )

    # {
    #   責務: [sample_spawn: live RNGと独立した固定seedで次の予測を一つ生成する]
    #   処理: [ローカルRandomだけを用い、pinned版と同じtile値→位置の順で抽選する]
    #   引数: [after_slide: apply_slideの結果, seed: 再現用の整数。boolは不可]
    #   戻り値: [Game2048State: 同じ入力とseedでは同じspawn。no-opなら元状態]
    #   エラー: [ValueError: 整数以外のseed、またはmoved盤面に空セルが無い]
    # }
    def sample_spawn(
        self, after_slide: ActionTransition[Game2048State], seed: int
    ) -> Game2048State:
        _validate_seed(seed)
        return (
            _sample_tile(after_slide.state, random.Random(seed))
            if after_slide.changed
            else after_slide.state
        )

    # {
    #   責務: [continue_game: 勝利後の仮想状態で追加操作を許可する]
    #   処理: [継続フラグだけを更新し、ゲームオーバーや盤面を改変しない]
    #   引数: [state: 勝利後を含む予測状態]
    #   戻り値: [Game2048State: 継続許可済みの新状態]
    # }
    def continue_game(self, state: Game2048State) -> Game2048State:
        return replace(state, keep_playing=True)

    # {
    #   責務: [restart: seed付きの独立した新規仮想ゲームを作る]
    #   処理: [点数と全フラグを初期化し、異なる空セルへ初期tileを二つ抽選する]
    #   引数: [seed: 再現用の整数。実ゲームのrestart操作ではない]
    #   戻り値: [Game2048State: 初期tile二つ、score=0の予測状態]
    #   エラー: [ValueError: 整数以外のseed]
    # }
    def restart(self, seed: int) -> Game2048State:
        _validate_seed(seed)
        rng = random.Random(seed)
        state = Game2048State(((0,) * BOARD_SIZE,) * BOARD_SIZE, score=0)
        return _sample_tile(_sample_tile(state, rng), rng)


# {
#   責務: [_line_positions: 移動先から順に4セルを列挙する]
#   処理: [上下は列、左右は行を選び、下と右は走査順を逆転する]
#   引数: [line: 行または列の番号0〜3, direction: 検証済みの方向]
#   戻り値: [tuple: 移動先から並ぶ行・列インデックス]
# }
def _line_positions(line: int, direction: str) -> tuple[tuple[int, int], ...]:
    indices = (
        range(BOARD_SIZE - 1, -1, -1)
        if direction in ("DOWN", "RIGHT")
        else range(BOARD_SIZE)
    )
    return tuple(
        (i, line) if direction in ("UP", "DOWN") else (line, i) for i in indices
    )


# {
#   責務: [_empty_positions: pinned Grid.availableCellsと同じ列優先順の空セルを返す]
#   処理: [盤面を変更せず0だけを列挙する]
#   引数: [state: spawn前の予測状態]
#   戻り値: [tuple: 空セルの行・列番号]
# }
def _empty_positions(state: Game2048State) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, col)
        for col in range(BOARD_SIZE)
        for row in range(BOARD_SIZE)
        if state.cells[row][col] == 0
    )


# {
#   責務: [_insert_tile: 一つの空セルへのspawnとspawn後の終了判定を行う]
#   処理: [盤面を複製し、空セルまたは隣接同値tileが存在すればプレイを継続する]
#   引数: [state: spawn前状態, row: 空セル行, col: 空セル列, value: 2または4]
#   戻り値: [Game2048State: spawn済みの新しい不変状態]
# }
def _insert_tile(state: Game2048State, row: int, col: int, value: int) -> Game2048State:
    cells = [list(line) for line in state.cells]
    cells[row][col] = value
    moves_available = any(
        cells[r][c] == 0
        or (r + 1 < BOARD_SIZE and cells[r][c] == cells[r + 1][c])
        or (c + 1 < BOARD_SIZE and cells[r][c] == cells[r][c + 1])
        for r in range(BOARD_SIZE)
        for c in range(BOARD_SIZE)
    )
    return replace(
        state, cells=tuple(tuple(line) for line in cells), over=not moves_available
    )


# {
#   責務: [_sample_tile: 呼出側が所有する乱数器で一つのspawnを抽選する]
#   処理: [tile値と列優先の空セルを独立抽選し、global RNGを進めない]
#   引数: [state: 空セルのある状態, rng: この仮想試行だけのRandom]
#   戻り値: [Game2048State: spawn後状態]
#   エラー: [ValueError: 空セルが無い]
# }
def _sample_tile(state: Game2048State, rng: random.Random) -> Game2048State:
    empty = _empty_positions(state)
    if not empty:
        raise ValueError("a moved 2048 board must have an empty cell before spawn")
    value = 2 if rng.random() < SPAWN_DISTRIBUTION[0][1] else 4
    row, col = empty[int(rng.random() * len(empty))]
    return _insert_tile(state, row, col, value)


# {
#   責務: [_validate_seed: 再現可能な明示整数seedだけを受け付ける]
#   処理: [暗黙の時刻seedやboolを拒否する]
#   引数: [seed: 呼出側が指定した試行seed]
#   戻り値: [なし]
#   エラー: [ValueError: seedが整数でない]
# }
def _validate_seed(seed: int) -> None:
    if type(seed) is not int:
        raise ValueError("simulation seed must be an integer")
