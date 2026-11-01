"""世界状態仮説のscope、証拠参照、鮮度、重みの小さな不変契約。"""

from dataclasses import dataclass, asdict
from enum import Enum

from ai_game_player.belief_validation import belief_id, belief_integer, belief_number


# {
#   責務: [BeliefWeightKind: 確率と比較score、確率が分からない状態を区別する]
#   フィールド: [値: probabilityだけが校正参照を持ち、scoreの正規化を確率と呼ばない]
# }
class BeliefWeightKind(str, Enum):
    CALIBRATED_PROBABILITY = "calibrated_probability"
    RELATIVE_SCORE = "relative_score"
    UNKNOWN = "unknown"


# {
#   責務: [BeliefEvidenceKind: 実観測の証拠とモデル予測・操作・結果の参照を区別する]
#   フィールド: [値: predictionをobservationへ昇格させない出所の分類]
# }
class BeliefEvidenceKind(str, Enum):
    OBSERVATION = "observation"
    PREDICTION = "prediction"
    ACTION = "action"
    OUTCOME = "outcome"


# {
#   責務: [BeliefContext: 混在を拒否するFrame/ゲーム/時計世代の一時点を表す]
#   フィールド: [game_id/scene_id/session_id: 対象実行scope, snapshot_id/frame_ids/frame_sequence: 観測の識別,
#     turn/action_epoch: ゲーム手番と実行世代, source_id/source_generation: frame producer世代,
#     clock_id: 同じmonotonic時計originを共有するopaque ID。別processの時刻は比較しない]
# }
@dataclass(frozen=True, slots=True)
class BeliefContext:
    game_id: str
    scene_id: str
    session_id: str
    snapshot_id: str
    frame_ids: tuple[str, ...]
    frame_sequence: int
    turn: int
    action_epoch: int
    source_id: str
    source_generation: int
    clock_id: str

    # {
    #   責務: [__post_init__: scopeと有界Frame参照を検証する]
    #   処理: [可変参照、重複Frame、不正ID/世代を拒否する]
    #   引数: [self: 構成中context]
    #   戻り値: [なし]
    #   エラー: [ValueError: scope契約違反]
    # }
    def __post_init__(self) -> None:
        for value in (
            self.game_id,
            self.scene_id,
            self.session_id,
            self.snapshot_id,
            self.source_id,
            self.clock_id,
        ):
            belief_id(value)
        if type(self.frame_ids) is not tuple or not 1 <= len(self.frame_ids) <= 16:
            raise ValueError(
                "belief context requires one to sixteen immutable frame refs"
            )
        for frame in self.frame_ids:
            belief_id(frame)
        if len(set(self.frame_ids)) != len(self.frame_ids):
            raise ValueError("belief context frame refs must be unique")
        for value in (
            self.frame_sequence,
            self.turn,
            self.action_epoch,
            self.source_generation,
        ):
            belief_integer(value)

    # {
    #   責務: [run_scope: 明示resetを必要とする実行scopeを取り出す]
    #   処理: [Frame/手番とは分けてgame/scene/session/source/clock世代を返す]
    #   引数: [self: 対象context]
    #   戻り値: [tuple: 境界をまたいで候補を混ぜない識別子]
    # }
    @property
    def run_scope(self) -> tuple:
        return (
            self.game_id,
            self.scene_id,
            self.session_id,
            self.source_id,
            self.source_generation,
            self.clock_id,
        )

    # {
    #   責務: [to_dict: scopeを機械可読metadataへ投影する]
    #   処理: [画像や自由文を含まない独立辞書を作る]
    #   引数: [self: 対象context]
    #   戻り値: [dict: JSON保存可能なscope]
    # }
    def to_dict(self) -> dict:
        return asdict(self)


# {
#   責務: [BeliefFreshness: 同じclock origin内の観測時刻と失効時刻を固定する]
#   フィールド: [observed_at_ns: 元証拠のmonotonic ns, expires_at_ns: この仮説の有効期限ns]
# }
@dataclass(frozen=True, slots=True)
class BeliefFreshness:
    observed_at_ns: int
    expires_at_ns: int

    # {
    #   責務: [__post_init__: 時刻の型とTTLの向きを検証する]
    #   処理: [bool/負数とゼロ以下のTTLを拒否する]
    #   引数: [self: 対象鮮度]
    #   戻り値: [なし]
    #   エラー: [ValueError: 不正な時刻]
    # }
    def __post_init__(self) -> None:
        belief_integer(self.observed_at_ns)
        belief_integer(self.expires_at_ns, self.observed_at_ns + 1)


# {
#   責務: [BeliefWeight: 校正参照付き確率と未校正比較scoreを別型タグで保持する]
#   フィールド: [kind: 重みの意味, value: finite非負値, calibration_id: 確率の外部校正/分布契約の版]
# }
@dataclass(frozen=True, slots=True)
class BeliefWeight:
    kind: BeliefWeightKind
    value: float
    calibration_id: str | None = None

    # {
    #   責務: [__post_init__: unknown値や校正なし確率を受け入れない]
    #   処理: [確率を0..1に限定し、scoreには校正済みの表示を付けない]
    #   引数: [self: 対象重み]
    #   戻り値: [なし]
    #   エラー: [ValueError: 重み種別・数値・校正参照の矛盾]
    # }
    def __post_init__(self) -> None:
        if self.kind not in (
            BeliefWeightKind.CALIBRATED_PROBABILITY,
            BeliefWeightKind.RELATIVE_SCORE,
        ) or not isinstance(self.kind, BeliefWeightKind):
            raise ValueError("hypothesis weight requires probability or relative score")
        maximum = (
            1.0
            if self.kind == BeliefWeightKind.CALIBRATED_PROBABILITY
            else float("inf")
        )
        object.__setattr__(self, "value", belief_number(self.value, maximum))
        if self.kind == BeliefWeightKind.CALIBRATED_PROBABILITY:
            belief_id(self.calibration_id)
        elif self.calibration_id is not None:
            raise ValueError("relative score must not claim probability calibration")


# {
#   責務: [BeliefEvidenceRef: 画像を持たず証拠の出所と相関groupを参照する]
#   フィールド: [evidence_id: 証拠ID, correlation_id: 同じ観測に由来する票をまとめるID,
#     kind: 観測/予測/操作/結果, source_id/source_version: recognizer/model等の版,
#     context: 証拠の対象scope, captured_at_ns: 同じclock originの時刻,
#     frame_id: observationだけが必須とする実Frame参照]
# }
@dataclass(frozen=True, slots=True)
class BeliefEvidenceRef:
    evidence_id: str
    correlation_id: str
    kind: BeliefEvidenceKind
    source_id: str
    source_version: str
    context: BeliefContext
    captured_at_ns: int
    frame_id: str | None = None

    # {
    #   責務: [__post_init__: 観測Frameの結び付きを検証する]
    #   処理: [予測に実Frame参照を付けず、観測にはcontext内Frameを要求する]
    #   引数: [self: 証拠参照]
    #   戻り値: [なし]
    #   エラー: [ValueError: 種別・Frame・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        for value in (
            self.evidence_id,
            self.correlation_id,
            self.source_id,
            self.source_version,
        ):
            belief_id(value)
        belief_integer(self.captured_at_ns)
        if not isinstance(self.context, BeliefContext) or not isinstance(
            self.kind, BeliefEvidenceKind
        ):
            raise ValueError("belief evidence context or kind is invalid")
        if self.kind == BeliefEvidenceKind.OBSERVATION:
            if self.frame_id not in self.context.frame_ids:
                raise ValueError(
                    "observed evidence requires a frame in the same context"
                )
        elif self.frame_id is not None:
            raise ValueError(
                "non-observation evidence must not claim an observed frame"
            )
