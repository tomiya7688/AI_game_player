"""2048の観測仮説・仮想遷移に使う不変状態。実ゲームの状態は取得しない。"""

from dataclasses import dataclass


BOARD_SIZE = 4
BoardCells = tuple[tuple[int, ...], ...]


# {
#   責務: [Game2048State: 画面由来の盤面仮説または予測盤面を、外部から変更できない値として保持する]
#   フィールド: [cells: 行優先の4×4盤面。0は空、それ以外は2以上の2の冪,
#     score: 観測または予測した累積点。未認識はNone,
#     won: 2048合成を経験した状態。大きいtileの存在だけでは推定しない,
#     over: 観測した終了状態、またはspawn後に合法手が無くなった予測,
#     keep_playing: 勝利後の継続が許可された状態]
# }
@dataclass(frozen=True, slots=True)
class Game2048State:
    cells: BoardCells
    score: int | None = None
    won: bool = False
    over: bool = False
    keep_playing: bool = False

    # {
    #   責務: [__post_init__: 可変入力の別名を残さず、盤面・得点・状態フラグの契約を検証する]
    #   処理: [4行4列と整数tileを検査し、各行をtupleに固定する。boolを整数として受理しない]
    #   引数: [self: 構築途中の状態]
    #   戻り値: [なし]
    #   エラー: [ValueError: 盤面形状、tile、得点または状態フラグが不正]
    # }
    def __post_init__(self) -> None:
        try:
            cells = tuple(tuple(row) for row in self.cells)
        except TypeError as error:
            raise ValueError(
                "2048 board must contain four rows of four cells"
            ) from error
        if len(cells) != BOARD_SIZE or any(len(row) != BOARD_SIZE for row in cells):
            raise ValueError("2048 board must contain four rows of four cells")
        if any(
            type(value) is not int
            or value < 0
            or value == 1
            or (value != 0 and value & (value - 1) != 0)
            for row in cells
            for value in row
        ):
            raise ValueError("2048 tiles must be zero or integer powers of two >= 2")
        if self.score is not None and (type(self.score) is not int or self.score < 0):
            raise ValueError("2048 score must be a non-negative integer or None")
        if any(
            type(flag) is not bool for flag in (self.won, self.over, self.keep_playing)
        ):
            raise ValueError("2048 status flags must be booleans")
        object.__setattr__(self, "cells", cells)

    # {
    #   責務: [terminated: pinned版の勝利停止とゲームオーバーを判定する]
    #   処理: [終了済み、または勝利済みで継続未許可なら操作を停止する]
    #   引数: [self: 判定対象の状態]
    #   戻り値: [bool: 仮想slideを受理しない状態ならTrue]
    # }
    @property
    def terminated(self) -> bool:
        return self.over or (self.won and not self.keep_playing)
