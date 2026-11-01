"""実観測と混ぜず、同じ観測参照へ固定する型付き数値候補予測。"""

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite

from ai_game_player.evaluation_primitives import PRIMITIVES
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier
from ai_game_player.models import ScreenObservation


# {
#   責務: [AxisPrediction: 観測済みと主張しない一軸の数値仮説と出所を保持する]
#   フィールド: [axis/value/confidence: 6軸の値と支持率, source_id/source_version: 明示予測器の参照]
# }
@dataclass(frozen=True, slots=True)
class AxisPrediction:
    axis: str
    value: float
    confidence: float
    source_id: str
    source_version: str

    # {
    #   責務: [__post_init__: 予測値・支持率・軸の意味を有限に制限する]
    #   処理: [penalty軸は非正、noveltyは非負、sourceはopaque参照だけを許す]
    #   引数: [self: 一軸仮説] 戻り値: [なし] エラー: [ValueError: 契約違反]
    # }
    def __post_init__(self) -> None:
        if (
            self.axis not in PRIMITIVES
            or type(self.value) not in (int, float)
            or not -1 <= self.value <= 1
            or not isfinite(self.value)
            or type(self.confidence) not in (int, float)
            or not 0 <= self.confidence <= 1
            or not isfinite(self.confidence)
            or (self.axis in {"repetition", "irreversible_loss"} and self.value > 0)
            or (self.axis == "novelty" and self.value < 0)
        ):
            raise ValueError("invalid bounded axis prediction")
        validate_identifier(self.source_id, "evaluation prediction source")
        validate_identifier(self.source_version, "evaluation prediction source version")


# {
#   責務: [CandidatePrediction: 現frame内の候補へ最大6つの異なる仮説軸を結び付ける]
#   フィールド: [action_id: 同じ観測の候補参照, axes: 未観測の数値仮説。実操作Outcomeを入れない]
# }
@dataclass(frozen=True, slots=True)
class CandidatePrediction:
    action_id: str
    axes: tuple[AxisPrediction, ...]

    # {
    #   責務: [__post_init__: mutable/重複/過大な候補仮説を拒否する]
    #   処理: [IDと6軸以内のtyped tupleを検証する]
    #   引数: [self: 候補仮説] 戻り値: [なし] エラー: [ValueError: 不正候補仮説]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self.action_id, str)
            or not self.action_id
            or len(self.action_id) > 128
            or type(self.axes) is not tuple
            or len(self.axes) > 6
            or any(type(axis) is not AxisPrediction for axis in self.axes)
            or len({axis.axis for axis in self.axes}) != len(self.axes)
        ):
            raise ValueError("invalid candidate evaluation prediction")


# {
#   責務: [EvaluationPredictionBatch: 外部で用意した仮説を一つの観測/scope/goal/版へ固定する]
#   フィールド: [version: 予測版, observation_ref: frame参照hash, scope/goal_id: 適用範囲,
#     candidates: 最大64候補の仮説。構成からの明示注入だけで採用する]
# }
@dataclass(frozen=True, slots=True)
class EvaluationPredictionBatch:
    version: str
    observation_ref: str
    scope: KnowledgeScope
    goal_id: str
    candidates: tuple[CandidatePrediction, ...]

    # {
    #   責務: [__post_init__: 異なるframe/範囲の再利用を検出できる有限batchを要求する]
    #   処理: [opaque版/参照・完全scope・候補数・一意性を検証する]
    #   引数: [self: 予測batch] 戻り値: [なし] エラー: [ValueError: 不正batch]
    # }
    def __post_init__(self) -> None:
        for value in (self.version, self.observation_ref, self.goal_id):
            validate_identifier(value, "evaluation batch reference")
        if (
            not isinstance(self.scope, KnowledgeScope)
            or type(self.candidates) is not tuple
            or len(self.candidates) > 64
            or any(type(item) is not CandidatePrediction for item in self.candidates)
            or len({item.action_id for item in self.candidates}) != len(self.candidates)
        ):
            raise ValueError("invalid immutable evaluation prediction batch")


# {
#   責務: [evaluation_observation_reference: 既存の画面/signature/frame IDを予測の再対応hashへ変換する]
#   処理: [生のlabel/画素を保持せず、参照欠落や過大な入力はNoneへ保留する]
#   引数: [observation: 実際に判断する同時点の観測] 戻り値: [str|None: boundedなframe参照]
# }
def evaluation_observation_reference(observation: ScreenObservation) -> str | None:
    signature = observation.features.get("signature")
    if (
        type(observation.screen_id) is not str
        or signature is not None
        and type(signature) is not str
    ):
        return None
    metadata = observation.features.get("capture_metadata", {})
    frame_id = metadata.get("frame_id") if type(metadata) is dict else None
    if type(frame_id) is int and not 0 <= frame_id <= 2**63 - 1:
        return None
    if not signature and frame_id is None:
        return None
    parts = (observation.screen_id, signature or "", frame_id)
    if any(
        item is not None and (type(item) not in (str, int) or len(str(item)) > 512)
        for item in parts
    ):
        return None
    encoded = json.dumps(parts, separators=(",", ":"), ensure_ascii=False).encode()
    return "frame." + sha256(encoded).hexdigest()
