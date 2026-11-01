"""Belief契約の有界識別子・数値・不変状態のcanonical化。"""

from dataclasses import fields, is_dataclass
from enum import Enum
import base64
import json
import math
import re


MAX_STATE_BYTES = 65536
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


# {
#   責務: [belief_id: 自由文・パスを含まない有界の参照IDを検証する]
#   処理: [型、文字種、長さと既知credential prefixを検査する]
#   引数: [value: 参照ID候補]
#   戻り値: [str: 検証したID]
#   エラー: [ValueError: ID契約違反]
# }
def belief_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or value.startswith(("sk-", "ghp_", "github_pat_"))
    ):
        raise ValueError("belief identifier is invalid")
    return value


# {
#   責務: [belief_integer: 世代・時刻・上限に使う整数をboolから区別する]
#   処理: [整数型と包含区間を検証する]
#   引数: [value: 対象値, minimum: 下限, maximum: 上限]
#   戻り値: [int: 検証した整数]
#   エラー: [ValueError: 型または区間違反]
# }
def belief_integer(value: object, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("belief integer is invalid")
    return value


# {
#   責務: [belief_number: finiteかつ非負の重み・質量を検証する]
#   処理: [bool、NaN、inf、負数を拒否する]
#   引数: [value: 重み候補, maximum: 任意上限]
#   戻り値: [float: 検証した値]
#   エラー: [ValueError: 不正な数値]
# }
def belief_number(value: object, maximum: float = float("inf")) -> float:
    if (
        type(value) not in (int, float)
        or (type(value) is int and value.bit_length() > 1024)
        or not math.isfinite(value)
        or not 0 <= value <= maximum
    ):
        raise ValueError("belief weight must be finite and non-negative")
    return float(value)


# {
#   責務: [belief_json: 不変契約のmachine-readable投影を決定的なUTF-8へ変換する]
#   処理: [NaNを拒否し、key順を固定する]
#   引数: [value: 検証済みJSON投影]
#   戻り値: [bytes: canonical JSON]
#   エラー: [ValueError/TypeError: JSONで表せない値]
# }
def belief_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


# {
#   責務: [canonical_belief_state: 型付き不変状態をserialize前に予算検査する]
#   処理: [循環、深さ、ノード数、scalar長、可変containerを拒否し、型tag付きcanonical JSONへ固定する]
#   引数: [state: frozen dataclass/tuple等だけで構成するゲーム状態]
#   戻り値: [bytes: 64KiB以下のcanonical typed state]
#   エラー: [ValueError: 可変状態、循環、NaN、サイズまたは深さ超過]
# }
def canonical_belief_state(state: object) -> bytes:
    active: set[int] = set()
    remaining = [8192, MAX_STATE_BYTES]

    # {
    #   責務: [encode: 一つの不変nodeを共有予算内でtyped JSONへ変換する]
    #   処理: [現在の経路の循環と累積node/scalar予算を先に検査する]
    #   引数: [value: node, depth: 現在の深さ]
    #   戻り値: [object: 有界のJSON node]
    #   エラー: [ValueError: 型または資源予算違反]
    # }
    def encode(value: object, depth: int) -> object:
        remaining[0] -= 1
        if depth > 16 or remaining[0] < 0:
            raise ValueError("belief state nesting or node budget exceeded")
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            if value.bit_length() > 256:
                raise ValueError("belief state integer exceeds scalar budget")
            return {"int": str(value)}
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("belief state number is not finite")
            return {"float": value.hex()}
        if type(value) in (str, bytes):
            if len(value) > remaining[1]:
                raise ValueError("belief state scalar byte budget exceeded")
            raw = value.encode("utf-8") if isinstance(value, str) else value
            remaining[1] -= len(raw)
            if remaining[1] < 0:
                raise ValueError("belief state scalar byte budget exceeded")
            return (
                {"str": value}
                if isinstance(value, str)
                else {"bytes": base64.b64encode(raw).decode("ascii")}
            )
        identity = id(value)
        if identity in active:
            raise ValueError("belief state contains a cycle")
        active.add(identity)
        try:
            if isinstance(value, Enum):
                return {
                    "enum": _type_name(type(value)),
                    "value": encode(value.value, depth + 1),
                }
            if type(value) in (tuple, frozenset):
                if len(value) > 2048:
                    raise ValueError("belief state container budget exceeded")
                items = [encode(item, depth + 1) for item in value]
                if type(value) is frozenset:
                    items.sort(key=belief_json)
                return {"tuple" if type(value) is tuple else "frozenset": items}
            if is_dataclass(value) and not isinstance(value, type):
                parameters = type(value).__dict__.get("__dataclass_params__")
                members = fields(value)
                if parameters is None or not parameters.frozen or len(members) > 64:
                    raise ValueError("belief record must be a bounded frozen dataclass")
                extra = getattr(value, "__dict__", {})
                if set(extra) - {item.name for item in members}:
                    raise ValueError("belief record has undeclared state fields")
                return {
                    "record": _type_name(type(value)),
                    "fields": [
                        [item.name, encode(getattr(value, item.name), depth + 1)]
                        for item in members
                    ],
                }
            raise ValueError("belief state contains a mutable or unsupported value")
        finally:
            active.remove(identity)

    encoded = belief_json(encode(state, 0))
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError("canonical belief state exceeds byte budget")
    return encoded


# {
#   責務: [_type_name: 型の名前を任意importに使わない有界metadataへ変換する]
#   処理: [開発コードのmodule/qualnameだけを採り長さを制限する]
#   引数: [state_type: 実在する型]
#   戻り値: [str: 型識別metadata]
#   エラー: [ValueError: 型名の長さ超過]
# }
def _type_name(state_type: type) -> str:
    result = f"{state_type.__module__}.{state_type.__qualname__}"
    if len(result) > 256:
        raise ValueError("belief state type name is too long")
    return result
