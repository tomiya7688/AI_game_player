"""一つの判断へ固定する6軸評価。actualとhypotheticalは別の欄に保持する。"""

from dataclasses import dataclass
import json
from math import isfinite
from typing import Any

from ai_game_player.evaluation_primitives import PRIMITIVES
from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.evaluation_profile import EvaluationProfile
from ai_game_player.evaluation_vector import quality_from_axes
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier


MAX_EVALUATION_BYTES = 128 * 1024


# {
#   責務: [RuntimeEvaluationProjection: 実Outcomeと候補仮説を分離したstep不変JSONを保持する]
#   フィールド: [_payload: 再検証済みcanonical bytes, expose_to_models: 数値文脈の明示送信許可]
# }
@dataclass(frozen=True, slots=True)
class RuntimeEvaluationProjection:
    _payload: bytes
    expose_to_models: bool = False
    _quality_cache: tuple[tuple[str, NumericQuality], ...] = ()
    _snapshot_id: str = ""
    _applied: bool = False

    # {
    #   責務: [from_dict: 6軸・quality・field/byte上限を検証してstep値を固定する]
    #   処理: [最大64候補、有限数値、unavailableゼロ支持、actual/hyp種別を確認する]
    #   引数: [value: adapterのlocal監査値, expose_to_models: 明示opt-in]
    #   戻り値: [RuntimeEvaluationProjection: 外部可変dictと分離した値]
    #   エラー: [ValueError: 契約・容量・数値不正]
    # }
    @classmethod
    def from_dict(
        cls, value: dict[str, Any], *, expose_to_models: bool = False
    ) -> "RuntimeEvaluationProjection":
        if (
            set(value)
            != {
                "schema",
                "context_snapshot_id",
                "status",
                "reason",
                "scope",
                "goal_id",
                "profile",
                "previous_actual",
                "candidate_hypothetical",
                "truncated_candidates",
            }
            or value["schema"] != "runtime-evaluation/v1"
            or not isinstance(value["context_snapshot_id"], str)
            or not value["context_snapshot_id"]
            or len(value["context_snapshot_id"]) > 128
            or type(value["candidate_hypothetical"]) is not dict
            or len(value["candidate_hypothetical"]) > 64
            or type(expose_to_models) is not bool
        ):
            raise ValueError(
                "runtime evaluation projection fields do not match contract"
            )
        profile = EvaluationProfile.from_dict(value["profile"])
        scope = (
            KnowledgeScope.from_dict(value["scope"])
            if value["scope"] is not None
            else None
        )
        for field in ("goal_id", "status", "reason"):
            validate_identifier(value[field], "evaluation projection metadata")
        if (
            type(value["truncated_candidates"]) is not int
            or value["truncated_candidates"] < 0
            or value["status"] == "applied"
            and not profile.matches(scope, value["goal_id"])
        ):
            raise ValueError("evaluation projection scope or omitted count mismatch")
        _validate_vector(value["previous_actual"], "previous_actual", profile)
        for action_id, vector in value["candidate_hypothetical"].items():
            if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
                raise ValueError("runtime evaluation candidate reference exceeds limit")
            _validate_vector(vector, "candidate_hypothetical", profile)
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(payload) > MAX_EVALUATION_BYTES:
            raise ValueError("runtime evaluation projection exceeds byte budget")
        qualities = tuple(
            (action_id, NumericQuality.from_dict(vector["quality"]))
            for action_id, vector in value["candidate_hypothetical"].items()
        )
        return cls(
            payload,
            expose_to_models,
            qualities,
            value["context_snapshot_id"],
            value["status"] == "applied",
        )

    # {
    #   責務: [to_dict: local Trace向けに全6軸と版・provenanceを独立コピーする]
    #   処理: [不変canonical bytesをJSONへ復元する]
    #   引数: [なし] 戻り値: [dict: コピー。保持値は変更されない]
    # }
    def to_dict(self) -> dict[str, Any]:
        return dict(json.loads(self._payload))

    # {
    #   責務: [quality_for: 次候補の仮説qualityだけをmodel-free provider/plannerへ渡す]
    #   処理: [actual欄は参照せず、異なるstep/候補/不採用は未知品質に戻す]
    #   引数: [action_id: 候補参照, context_snapshot_id: 同じ判断の参照]
    #   戻り値: [NumericQuality: 候補仮説または未知]
    # }
    def quality_for(self, action_id: str, context_snapshot_id: str) -> NumericQuality:
        return self.candidate_qualities(context_snapshot_id).get(
            action_id, NumericQuality()
        )

    # {
    #   責務: [candidate_qualities: 同stepの候補仮説を一度の有限copyで取得する]
    #   処理: [actualを含めず、キャッシュした不変品質だけを最大64件返す]
    #   引数: [context_snapshot_id: 対象step] 戻り値: [dict: 候補IDからNumericQuality]
    # }
    def candidate_qualities(
        self, context_snapshot_id: str
    ) -> dict[str, NumericQuality]:
        return (
            dict(self._quality_cache)
            if self._applied and self._snapshot_id == context_snapshot_id
            else {}
        )

    # {
    #   責務: [to_model_dict: opt-in時の数値要約をscope/receipt/private profile情報なしで返す]
    #   処理: [候補qualityだけを4KiB以内に限定する。省略候補を非許可へ変更しない]
    #   引数: [なし] 戻り値: [dict: bounded model summary]
    # }
    def to_model_dict(self) -> dict[str, Any]:
        value = self.to_dict()
        result: dict[str, Any] = {
            "schema": value["schema"],
            "context_snapshot_id": value["context_snapshot_id"],
            "status": value["status"],
            "candidates": {},
            "omitted": len(value["candidate_hypothetical"]),
        }
        for action_id, vector in value["candidate_hypothetical"].items():
            quality = vector["quality"]
            result["candidates"][action_id] = {
                name: quality[name]
                for name in (
                    "value",
                    "confidence",
                    "coverage",
                    "available",
                    "missing_axes",
                )
            }
            result["omitted"] -= 1
            if len(json.dumps(result, separators=(",", ":")).encode()) > 4096:
                del result["candidates"][action_id]
                result["omitted"] += 1
                break
        return result


# {
#   責務: [_validate_vector: actual/hyp種別と欠損を含む全6軸を検証する]
#   処理: [quality契約・axis数値・availabilityを照合する]
#   引数: [vector: 値, kind: 期待する由来, profile: 適用weightと版] 戻り値: [なし]
#   エラー: [ValueError: field/数値/由来不正]
# }
def _validate_vector(vector: object, kind: str, profile: EvaluationProfile) -> None:
    if (
        not isinstance(vector, dict)
        or not {"kind", "status", "axes", "quality"} <= set(vector)
        or set(vector)
        - {
            "kind",
            "status",
            "axes",
            "quality",
            "reference",
            "perceptual_cues",
            "profile",
        }
        or vector.get("kind") != kind
        or set(vector.get("axes", {})) != set(PRIMITIVES)
    ):
        raise ValueError("evaluation vector axes or origin mismatch")
    if kind == "previous_actual":
        if "profile" not in vector:
            raise ValueError("actual evaluation must retain the original profile")
        profile = EvaluationProfile.from_dict(vector["profile"])
    elif "profile" in vector:
        raise ValueError("candidate evaluation must use the current profile")
    validate_identifier(vector["status"], "evaluation vector status")
    quality = NumericQuality.from_dict(vector["quality"])
    if quality.to_dict() != vector["quality"]:
        raise ValueError("evaluation quality must preserve canonical missingness")
    for axis in vector["axes"].values():
        if (
            type(axis) is not dict
            or set(axis)
            != {
                "value",
                "confidence",
                "available",
                "source_id",
                "source_version",
                "evidence_kind",
            }
            or type(axis.get("available")) is not bool
            or any(
                type(axis.get(key)) not in (int, float)
                for key in ("value", "confidence")
            )
            or not -1 <= axis["value"] <= 1
            or not 0 <= axis["confidence"] <= 1
            or any(not isfinite(axis[key]) for key in ("value", "confidence"))
            or (
                not axis["available"]
                and (axis["value"] != 0 or axis["confidence"] != 0)
            )
        ):
            raise ValueError("invalid axis availability or value")
        if axis["available"]:
            validate_identifier(axis.get("source_id"), "evaluation axis source")
            validate_identifier(
                axis.get("source_version"), "evaluation axis source version"
            )
            expected = (
                {"hypothetical_prediction"}
                if kind == "candidate_hypothetical"
                else {"observed_metric", "perceptual_cue", "terminal_pattern_inference"}
            )
            if axis.get("evidence_kind") not in expected:
                raise ValueError(
                    "evaluation axis evidence kind crosses actual/prediction boundary"
                )
        elif (
            axis.get("evidence_kind") != "unavailable"
            or axis["source_id"] is not None
            or axis["source_version"] is not None
        ):
            raise ValueError("unavailable axis must preserve missing evidence kind")
    _validate_reference(vector, kind)
    if quality != quality_from_axes(vector["axes"], profile):
        raise ValueError("evaluation quality does not match axes and weights")


# {
#   責務: [_validate_reference: raw説明やactual参照を候補予測へ混ぜない有限監査形を検証する]
#   処理: [参照fieldとopaque ID、perceptual cueの固定分類だけを受け付ける]
#   引数: [vector: 検証するvector, kind: actual/hypotheticalの由来] 戻り値: [なし]
#   エラー: [ValueError: 参照/由来/監査形不正]
# }
def _validate_reference(vector: dict[str, Any], kind: str) -> None:
    reference = vector.get("reference")
    if kind == "candidate_hypothetical":
        if (
            type(reference) is not dict
            or set(reference) != {"prediction_version", "observation_ref"}
            or "perceptual_cues" in vector
        ):
            raise ValueError("candidate evaluation requires hypothetical references")
        values = list(reference.values())
        if any(value is None for value in values) and any(
            value is not None for value in values
        ):
            raise ValueError("candidate prediction references must be paired")
        for value in values:
            if value is not None:
                validate_identifier(value, "candidate prediction reference")
    elif reference is not None:
        if (
            type(reference) is not dict
            or set(reference)
            != {
                "snapshot_id",
                "action_ref",
                "action_generation",
                "after_screen_ref",
                "after_state_ref",
            }
            or type(reference["action_generation"]) is not int
            or reference["action_generation"] < 0
        ):
            raise ValueError("actual evaluation receipt reference mismatch")
        for key in ("snapshot_id", "action_ref", "after_screen_ref", "after_state_ref"):
            validate_identifier(reference[key], "actual evaluation reference")
    cues = vector.get("perceptual_cues", [])
    if type(cues) is not list or len(cues) > 8:
        raise ValueError("perceptual cue budget exceeded")
    for cue in cues:
        if (
            type(cue) is not dict
            or set(cue) != {"source", "classification", "meaning"}
            or cue["source"] not in {"screen_diff", "state_delta"}
            or cue["classification"] not in {"changed", "stable", "unknown"}
            or cue["meaning"] != "perceptual_change_not_semantic_progress"
        ):
            raise ValueError("perceptual cue must not contain semantic or raw claims")
