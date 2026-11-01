"""Canonical Experienceの操作効果eventを、数値集計向けに検証して投影する。"""

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite

from ai_game_player.experience import ExperienceEvent
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier
from ai_game_player.knowledge_snapshot import (
    MAX_COUNT,
    THRESHOLD_HINT_KEYS,
    WEIGHT_HINT_KEYS,
)


OUTCOME_EVENT_TYPE = "knowledge.action_outcome"
OUTCOMES = frozenset({"success", "no_effect", "failure", "unknown"})
EXECUTION_STATUSES = frozenset(
    {
        "executed",
        "dispatch_unknown",
        "dry_run",
        "planned",
        "cancelled",
        "before_dispatch_failure",
    }
)
REQUIRED_FIELDS = frozenset(
    {
        "scope",
        "source_id",
        "source_sequence",
        "correlation_id",
        "correlation_sequence",
        "action_id",
        "outcome",
        "evidence_kind",
        "verified",
        "confidence",
        "execution_status",
        "action_generation",
    }
)
OPTIONAL_FIELDS = frozenset(
    {
        "candidate_reliability",
        "from_state",
        "to_state",
        "weight_hints",
        "threshold_hints",
        "roi_hints",
    }
)


# {
#   責務: [KnowledgeUpdateSample: 操作との対応と証拠の出所を保持する、変更不能な集計用投影]
#   フィールド: [event_id: 元Experience eventの識別子, scope: game/scene/profile/Player系統,
#     source_id/source_sequence: 再投入を排除する永続sourceと単調順序,
#     correlation_id/correlation_sequence: 同じ操作の複数frame・検出器を一件に制限する系列と順序,
#     action_id/outcome: 対象操作と明示された効果分類, evidence_kind/provenance_source/verified/confidence: 効果採用の根拠,
#     execution_status/action_generation: 実行receiptに結び付く入力送信状態と操作世代,
#     candidate_reliability: 任意の外部認識校正値, transition: 任意の前後状態ID,
#     weight_hints/threshold_hints/roi_hints: Safetyに適用しない数値候補, evidence_digest: 元証拠の不透明な指紋]
# }
@dataclass(frozen=True, slots=True)
class KnowledgeUpdateSample:
    event_id: str
    scope: KnowledgeScope
    source_id: str
    source_sequence: int
    correlation_id: str
    correlation_sequence: int
    action_id: str
    outcome: str
    evidence_kind: str
    provenance_source: str | None
    verified: bool
    confidence: float
    execution_status: str
    action_generation: int
    candidate_reliability: float | None
    transition: tuple[str, str] | None
    weight_hints: tuple[tuple[str, float], ...]
    threshold_hints: tuple[tuple[str, float], ...]
    roi_hints: tuple[tuple[str, tuple[float, ...]], ...]
    evidence_digest: str

    # {
    #   責務: [from_event: 明示型のExperience eventだけを数値Knowledge用の入力へ変換する]
    #   処理: [宣言済みフィールド、隔離scope、順序、確信度、数値hintを検証し、自由文や未加工画面を保持しない]
    #   引数: [event: canonical ExperienceEvent。source_idはreader名ではなく永続するsession/episode ID]
    #   戻り値: [KnowledgeUpdateSample: 信頼性判定前の型付き証拠投影]
    #   エラー: [ValueError: event型、フィールド、識別子、数値またはhintが不正]
    # }
    @classmethod
    def from_event(cls, event: ExperienceEvent) -> "KnowledgeUpdateSample":
        if (
            not isinstance(event, ExperienceEvent)
            or event.event_type != OUTCOME_EVENT_TYPE
        ):
            raise ValueError(
                "knowledge builder requires a canonical action-outcome event"
            )
        value = dict(event.payload)
        if (
            not REQUIRED_FIELDS <= set(value)
            or set(value) - REQUIRED_FIELDS - OPTIONAL_FIELDS
        ):
            raise ValueError(
                "action-outcome fields do not match the knowledge contract"
            )
        for name in ("source_id", "correlation_id", "action_id", "evidence_kind"):
            validate_identifier(value[name], name)
        validate_identifier(event.event_id, "event identifier")
        validate_identifier(event.source, "event source")
        scope = KnowledgeScope.from_dict(value["scope"])
        for name in ("source_sequence", "correlation_sequence", "action_generation"):
            if type(value[name]) is not int or not 0 <= value[name] <= MAX_COUNT:
                raise ValueError(f"{name} must be a bounded non-negative integer")
        if (
            not isinstance(value["outcome"], str)
            or value["outcome"] not in OUTCOMES
            or type(value["verified"]) is not bool
        ):
            raise ValueError("action-outcome classification is invalid")
        if (
            not isinstance(value["execution_status"], str)
            or value["execution_status"] not in EXECUTION_STATUSES
        ):
            raise ValueError("action-outcome execution status is invalid")
        if value["action_generation"] != value["correlation_sequence"]:
            raise ValueError("action generation must match its correlation sequence")
        confidence = _number(value["confidence"], 0, 1, "confidence")
        provenance = event.provenance
        if (
            provenance is None
            or provenance.confidence is None
            or provenance.evidence_event_id is None
        ):
            confidence = 0.0
        else:
            validate_identifier(provenance.source, "provenance source")
            validate_identifier(
                provenance.evidence_event_id, "evidence event identifier"
            )
            confidence = min(confidence, float(provenance.confidence))
        reliability = value.get("candidate_reliability")
        if reliability is not None:
            reliability = _number(reliability, 0, 1, "candidate reliability")
        transition = None
        if "from_state" in value or "to_state" in value:
            if "from_state" not in value or "to_state" not in value:
                raise ValueError("transition requires both state identifiers")
            transition = (
                validate_identifier(value["from_state"], "from_state"),
                validate_identifier(value["to_state"], "to_state"),
            )
        weights = _hints(
            value.get("weight_hints", {}), WEIGHT_HINT_KEYS, 10, "weight hints"
        )
        thresholds = _hints(
            value.get("threshold_hints", {}), THRESHOLD_HINT_KEYS, 1, "threshold hints"
        )
        rois = []
        roi_values = value.get("roi_hints", {})
        if not isinstance(roi_values, dict) or len(roi_values) > 64:
            raise ValueError("ROI hints must be a bounded object")
        for key in roi_values:
            validate_identifier(key, "ROI identifier")
        for key, rectangle in sorted(roi_values.items()):
            validate_identifier(key, "ROI identifier")
            if not isinstance(rectangle, (tuple, list)) or len(rectangle) != 4:
                raise ValueError("ROI must contain normalized x/y/width/height")
            coords = tuple(_number(item, 0, 1, "ROI coordinate") for item in rectangle)
            if (
                coords[2] <= 0
                or coords[3] <= 0
                or coords[0] + coords[2] > 1
                or coords[1] + coords[3] > 1
            ):
                raise ValueError("ROI must be a non-empty rectangle within the frame")
            rois.append((key, coords))
        evidence_digest = sha256(
            json.dumps(
                {
                    "event_id": event.event_id,
                    "source": event.source,
                    "payload": value,
                    "provenance_source": None
                    if provenance is None
                    else provenance.source,
                    "evidence_event_id": None
                    if provenance is None
                    else provenance.evidence_event_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        return cls(
            event.event_id,
            scope,
            value["source_id"],
            value["source_sequence"],
            value["correlation_id"],
            value["correlation_sequence"],
            value["action_id"],
            value["outcome"],
            value["evidence_kind"],
            None if provenance is None else provenance.source,
            value["verified"],
            confidence,
            value["execution_status"],
            value["action_generation"],
            reliability,
            transition,
            weights,
            thresholds,
            tuple(rois),
            evidence_digest,
        )


# {
#   責務: [_number: 数値証拠を有限な指定範囲へ制限する]
#   処理: [bool、非数値、非有限値と範囲外を拒否する]
#   引数: [value: 入力値, minimum/maximum: 許容範囲, name: 固定の検証項目名]
#   戻り値: [float: 検証済み数値]
#   エラー: [ValueError: 不正な数値]
# }
def _number(value: object, minimum: float, maximum: float, name: str) -> float:
    if (
        type(value) not in (int, float)
        or not minimum <= value <= maximum
        or not isfinite(value)
    ):
        raise ValueError(f"{name} must be a finite bounded number")
    return float(value)


# {
#   責務: [_hints: Safety設定を含まない宣言済み数値hintだけを検証する]
#   処理: [許可キーと値域を照合し、順序固定のtupleへ変換する]
#   引数: [value: hint辞書, keys: 許可キー集合, maximum: 数値上限, name: 固定項目名]
#   戻り値: [tuple: 独立した不変hint一覧]
#   エラー: [ValueError: 未宣言のキーまたは不正値]
# }
def _hints(
    value: object, keys: frozenset[str], maximum: float, name: str
) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, dict) or set(value) - keys:
        raise ValueError(f"{name} contain unsupported fields")
    return tuple(
        (key, _number(item, 0, maximum, name)) for key, item in sorted(value.items())
    )
