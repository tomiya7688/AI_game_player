"""既存PrimitiveEvaluatorの軸算術を使い、欠損と全weight分母を保持する。"""

from hashlib import sha256
from typing import Any

from ai_game_player.evaluation_prediction import AxisPrediction
from ai_game_player.evaluation_primitives import PrimitiveEvaluator, PRIMITIVES
from ai_game_player.evaluation_profile import EvaluationProfile
from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.outcome import OutcomeAssessment


SIGNAL_KEYS = {"progress": "progress_delta", "resources": "resource_delta"}


# {
#   責務: [evaluate_runtime_vector: typed数値根拠だけを既存6軸と不確かさ付き品質へ変換する]
#   処理: [欠損survival既定値を抑制し、全weightを分母にcoverage/confidenceを保存する。
#     条件付き値と支持率を別に返し、実Outcomeを次候補予測へ転記しない]
#   引数: [evidence: 最大6軸のtyped根拠, profile: 選択済みweight方針,
#     kind: actual/hypotheticalの由来, status: 固定理由code]
#   戻り値: [dict: 6軸availability・出所・NumericQualityを含む純粋な値]
# }
def evaluate_runtime_vector(
    evidence: tuple[AxisPrediction, ...],
    profile: EvaluationProfile,
    *,
    kind: str,
    status: str,
) -> dict[str, Any]:
    by_axis = {item.axis: item for item in evidence if item.confidence > 0}
    signals = {}
    for name, predicted in by_axis.items():
        signals[SIGNAL_KEYS.get(name, name)] = (
            -predicted.value
            if name in {"repetition", "irreversible_loss"}
            else predicted.value
        )
        signals[f"{name}_confidence"] = predicted.confidence
    evaluated = PrimitiveEvaluator(dict(profile.weights)).evaluate(
        OutcomeAssessment("unknown", 0.0, "typed runtime evaluation evidence"), signals
    )
    axes = {}
    for name in PRIMITIVES:
        item = by_axis.get(name)
        measured = evaluated.axes[name]
        value = measured.value if item is not None else 0.0
        confidence = measured.confidence if item is not None else 0.0
        axes[name] = {
            "value": value,
            "confidence": confidence,
            "available": item is not None,
            "source_id": item.source_id if item else None,
            "source_version": item.source_version if item else None,
            "evidence_kind": (
                "unavailable"
                if item is None
                else "hypothetical_prediction"
                if kind == "candidate_hypothetical"
                else {
                    "observed_state_metric": "observed_metric",
                    "observed_state_cue": "perceptual_cue",
                    "observed_terminal_pattern": "terminal_pattern_inference",
                }.get(item.source_id, "observed_metric")
            ),
        }
    quality = quality_from_axes(axes, profile)
    return {"kind": kind, "status": status, "axes": axes, "quality": quality.to_dict()}


# {
#   責務: [quality_from_axes: 6軸から欠損を残す同一MCDA計算を実行する]
#   処理: [全weightを分母にして支持率/coverageを計算し、条件付き値へ分離する]
#   引数: [axes: 検証済み6軸, profile: 適用方針] 戻り値: [NumericQuality: 不変の数値品質]
# }
def quality_from_axes(
    axes: dict[str, dict[str, Any]], profile: EvaluationProfile
) -> NumericQuality:
    support = weighted_value = available_weight = 0.0
    total = sum(weight for _, weight in profile.weights)
    refs: list[str] = []
    missing: list[str] = []
    for name, weight in profile.weights:
        axis = axes[name]
        if not axis["available"]:
            missing.append(name)
            continue
        support += weight * axis["confidence"]
        weighted_value += weight * axis["confidence"] * axis["value"]
        available_weight += weight
        refs.append(
            "evidence."
            + sha256(
                f"{axis['source_id']}\0{axis['source_version']}".encode()
            ).hexdigest()
        )
    available = support > 0 and total > 0
    return NumericQuality(
        weighted_value / support if available else 0.0,
        support / total if available else 0.0,
        available,
        profile_version=profile.version,
        provenance=tuple(sorted(set(refs)))[:8],
        coverage=available_weight / total if total else 0.0,
        missing_axes=tuple(missing),
    )
