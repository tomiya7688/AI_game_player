"""保存証拠のJSONと比較元の型を検証する。画素や認識器の状態は変更しない。"""

from __future__ import annotations

import json
from math import isfinite


# v1 evidenceの保存形式を検証する寸法であり、実行時の認識閾値ではない。
EVIDENCE_BOARD_DIMENSION = 4


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"manifestにJSON標準外の数値{value}があります。元artifactを確認してください")


def _parse_finite_float(value: str) -> float:
    """指数表記もfloat変換後に検査し、1e999のように無限大になる入力を拒否する。"""
    number = float(value)
    if not isfinite(number):
        raise ValueError("manifestの数値を有限のfloatで表現できません。元artifactを確認してください")
    return number


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """各JSON objectの重複キーを拒否し、後勝ちで証拠が読み替わることを防ぐ。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"manifestのJSON objectに重複キー{key[:80]!r}があります。元artifactを確認してください")
        result[key] = value
    return result


def parse_evidence_json(encoded: bytes) -> object:
    """上限検査済みのbytesを、全階層の重複キー・非有限floatを拒否して読む。

    schemaとframe集合は呼び出し側で検証する。未知の診断項目は保持し、
    入力の正規化、欠落値の補完、比較値の型変換は行わない。
    """
    return json.loads(
        encoded,
        parse_constant=_reject_nonfinite_json,
        parse_float=_parse_finite_float,
        object_pairs_hook=_unique_object,
    )


def validate_recorded_visual_state(role: str, recorded: object) -> None:
    """board/confidenceの型を比較前に検査し、型違いを認識結果の差と区別する。

    欠落項目は比較不能、boardのnullは未検出、cellのnullは不明として許可する。
    confidenceのbool/null、cellのboolや整数相当floatは許可しない。
    OCR依存のscoreなど、比較しない項目の型はこの関数で限定しない。
    """
    if not isinstance(recorded, dict):
        raise ValueError(f"{role}: game_stateがJSON objectではありません")
    if "confidence" in recorded:
        confidence = recorded["confidence"]
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError(f"{role}: confidenceには0から1の有限数が必要です。boolやnullは使えません")
    board = recorded.get("board")
    if board is None:
        return
    if not isinstance(board, list) or len(board) != EVIDENCE_BOARD_DIMENSION:
        raise ValueError(f"{role}: boardにはnullまたは4x4の配列が必要です")
    for row_index, row in enumerate(board):
        if not isinstance(row, list) or len(row) != EVIDENCE_BOARD_DIMENSION:
            raise ValueError(f"{role}: board[{row_index}]には4個のcellが必要です")
        for column_index, value in enumerate(row):
            if value is None:
                continue
            # 2の累乗はbitが一つだけなので、直前の整数とのANDが0になる。
            if type(value) is int and (value == 0 or (value >= 2 and value & (value - 1) == 0)):
                continue
            raise ValueError(
                f"{role}: board[{row_index}][{column_index}]にはnull、整数0、"
                "または2以上の2の累乗整数が必要です"
            )
