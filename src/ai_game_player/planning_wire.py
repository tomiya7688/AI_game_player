"""有界JSON file IPCとtyped resultの投影。任意pickleや動的state importはしない。"""

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.belief_contract import BeliefContext
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_result import (
    HypothesisActionValue,
    PlanningActionValue,
    PlanningCounters,
    PlanningResult,
    WeightedValue,
)


# {
#   責務: [publish_planning_json: 小さい既存atomic JSON基盤で完全なmessageだけを公開する]
#   処理: [serialize後の実byte予算を検査し、同一directoryの一時fileを原子的に置換する]
#   引数: [path: worker所有通信先, value: 検証済みmessage, maximum: 実file byte上限]
#   戻り値: [なし] エラー: [ValueError/OSError: サイズ/JSON/IO失敗]
# }
def publish_planning_json(path: Path, value: dict[str, Any], maximum: int) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode(
        "utf-8"
    )
    if len(encoded) > maximum:
        raise ValueError("planning message byte budget exceeded")
    staged = stage_json_write(path, value)
    try:
        staged.publish()
    finally:
        staged.discard()


# {
#   責務: [read_planning_json: 信頼された一時directory内のmessageを有界に読む]
#   処理: [最大+1byteだけ読み、重複key/NaN/非objectを拒否する]
#   引数: [path: message先, maximum: byte上限] 戻り値: [dict: 独立JSON object]
#   エラー: [OSError/ValueError: 未公開/破損/サイズ超過]
# }
def read_planning_json(path: Path, maximum: int) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("planning message byte budget exceeded")
    result = json.loads(
        raw, object_pairs_hook=_unique, parse_constant=_invalid_constant
    )
    if not isinstance(result, dict):
        raise ValueError("planning message must be an object")
    return result


# {
#   責務: [_unique: JSONの重複keyを拒否して上書き解釈を防ぐ]
#   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
#   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
#   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
# }
def _unique(pairs: list[tuple[str, object]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate planning message key")
        result[key] = value
    return result


# {
#   責務: [_invalid_constant: NaN/InfinityのJSON入力を拒否する]
#   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
#   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
#   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
# }
def _invalid_constant(_value: str) -> object:
    raise ValueError("non-finite planning message")


# {
#   責務: [context_from_dict: Frame/時計scopeを許可済みfieldだけで復元する]
#   処理: [追加fieldや可変Frame列の長さを先に検査し、不変contextへ変換する]
#   引数: [value: JSON scope] 戻り値: [BeliefContext: 同じsource/clock識別]
#   エラー: [ValueError/TypeError: 契約違反]
# }
def context_from_dict(value: object) -> BeliefContext:
    fields = {
        "game_id",
        "scene_id",
        "session_id",
        "snapshot_id",
        "frame_ids",
        "frame_sequence",
        "turn",
        "action_epoch",
        "source_id",
        "source_generation",
        "clock_id",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or not isinstance(value["frame_ids"], list)
        or not 1 <= len(value["frame_ids"]) <= 16
    ):
        raise ValueError("planning context fields are invalid")
    return BeliefContext(**{**value, "frame_ids": tuple(value["frame_ids"])})


# {
#   責務: [result_to_dict: 不変resultを出所と区間を保った独立JSON投影へ変換する]
#   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
#   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
#   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
# }
def result_to_dict(result: PlanningResult) -> dict[str, Any]:
    return asdict(result)


# {
#   責務: [result_from_dict: fieldとcontainer上限を先に照合しtyped resultを再検証する]
#   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
#   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
#   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
# }
def result_from_dict(value: object) -> PlanningResult:
    fields = set(PlanningResult.__dataclass_fields__)
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or not isinstance(value["action_values"], list)
        or len(value["action_values"]) > 32
        or not isinstance(value["allowed_action_ids"], list)
        or len(value["allowed_action_ids"]) > 32
    ):
        raise ValueError("planning result fields are invalid")
    actions = []
    for item in value["action_values"]:
        if (
            not isinstance(item, dict)
            or set(item) != set(PlanningActionValue.__dataclass_fields__)
            or not isinstance(item["hypothesis_values"], list)
            or len(item["hypothesis_values"]) > 256
        ):
            raise ValueError("planning action result is invalid")
        hypotheses = []
        for hypothesis in item["hypothesis_values"]:
            if not isinstance(hypothesis, dict) or set(hypothesis) != set(
                HypothesisActionValue.__dataclass_fields__
            ):
                raise ValueError("planning hypothesis result is invalid")
            hypotheses.append(
                HypothesisActionValue(
                    **{
                        **hypothesis,
                        "conditional_outcomes": _trace_from(
                            hypothesis["conditional_outcomes"]
                        ),
                    }
                )
            )
        actions.append(
            PlanningActionValue(
                **{
                    **item,
                    "outcomes": _trace_from(item["outcomes"]),
                    "hypothesis_values": tuple(hypotheses),
                }
            )
        )
    counters = value["counters"]
    if not isinstance(counters, dict) or set(counters) != set(
        PlanningCounters.__dataclass_fields__
    ):
        raise ValueError("planning counters fields are invalid")
    return PlanningResult(
        **{
            **value,
            "outcome": PlanningOutcome(value["outcome"]),
            "context": context_from_dict(value["context"]),
            "action_values": tuple(actions),
            "allowed_action_ids": tuple(value["allowed_action_ids"]),
            "counters": PlanningCounters(**counters),
        }
    )


# {
#   責務: [_trace_from: 有界なleaf配列を数値検証した不変tupleへ変換する]
#   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
#   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
#   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
# }
def _trace_from(value: object) -> tuple[WeightedValue, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 256
        or any(
            not isinstance(item, dict)
            or set(item) != set(WeightedValue.__dataclass_fields__)
            for item in value
        )
    ):
        raise ValueError("planning distribution fields are invalid")
    return tuple(WeightedValue(**item) for item in value)
