"""実操作後の根拠と未実行候補の仮説を分離して6軸へ投影する本番adapter。"""

from collections import OrderedDict
from dataclasses import replace
from hashlib import sha256
from math import isfinite
import threading
from typing import Any, cast

from ai_game_player.decision_context import DecisionContext
from ai_game_player.evaluation_prediction import (
    AxisPrediction,
    EvaluationPredictionBatch,
    evaluation_observation_reference,
)
from ai_game_player.evaluation_profile import EvaluationProfile
from ai_game_player.evaluation_vector import evaluate_runtime_vector
from ai_game_player.execution_receipt import ExecutionObservation
from ai_game_player.knowledge_scope import KnowledgeScope, validate_identifier
from ai_game_player.models import ScreenObservation
from ai_game_player.runtime_evaluation_projection import RuntimeEvaluationProjection


# {
#   責務: [RuntimeEvaluationAdapter: 同stepの価値基準・actual・hypotheticalを限定量で固定する]
#   フィールド: [_profile/_scope/_goal_id: 信頼済み構成の方針, _predictions: 明示注入の不変仮説,
#     _lock: 短いmemory capture用, _bindings: 直近32判断のscope/goal/元profile,
#     _expose_to_models: 明示送信許可]
# }
class RuntimeEvaluationAdapter:
    # {
    #   責務: [__init__: 外部推論やIOを始めず型付き設定だけを保持する]
    #   処理: [raw featuresの予測やscene自己申告を読まず、既定の汎用weightを用意する]
    #   引数: [profile: 任意の信頼済み方針, scope/goal_id: hostが選んだ範囲,
    #     predictions: 任意の同frame数値仮説, expose_to_models: 既定Falseの明示opt-in]
    #   戻り値: [なし] エラー: [ValueError: 設定型/参照不正]
    # }
    def __init__(
        self,
        profile: EvaluationProfile | None = None,
        *,
        scope: KnowledgeScope | None = None,
        goal_id: str = "unresolved",
        predictions: EvaluationPredictionBatch | None = None,
        expose_to_models: bool = False,
    ) -> None:
        if (
            profile is not None
            and type(profile) is not EvaluationProfile
            or scope is not None
            and type(scope) is not KnowledgeScope
            or predictions is not None
            and type(predictions) is not EvaluationPredictionBatch
            or type(expose_to_models) is not bool
        ):
            raise ValueError("runtime evaluation requires typed configuration")
        validate_identifier(goal_id, "runtime evaluation goal")
        self._profile = profile or EvaluationProfile()
        self._scope = scope
        self._goal_id = goal_id
        self._predictions = predictions
        self._expose_to_models = expose_to_models
        self._lock = threading.Lock()
        self._bindings: OrderedDict[str, tuple[str | None, str, EvaluationProfile]] = (
            OrderedDict()
        )

    # {
    #   責務: [select_profile: 次stepから使う方針をmemoryだけで選び直す]
    #   処理: [既存projection/allowed/Safetyを変更しない]
    #   引数: [profile: 検証済み不変方針] 戻り値: [なし] エラー: [ValueError: 型不正]
    # }
    def select_profile(self, profile: EvaluationProfile) -> None:
        if type(profile) is not EvaluationProfile:
            raise ValueError("runtime evaluation profile must be immutable")
        with self._lock:
            self._profile = profile

    # {
    #   責務: [set_predictions: hostが別経路で得た同frame仮説を次capture用に保持する]
    #   処理: [推論callbackを受けず、typed immutable batchの参照だけ交換する]
    #   引数: [predictions: 任意の数値仮説] 戻り値: [なし] エラー: [ValueError: 型不正]
    # }
    def set_predictions(self, predictions: EvaluationPredictionBatch | None) -> None:
        if (
            predictions is not None
            and type(predictions) is not EvaluationPredictionBatch
        ):
            raise ValueError("runtime evaluation predictions must be immutable")
        with self._lock:
            self._predictions = predictions

    # {
    #   責務: [select_context: 信頼済みhostが次stepのscene/goal範囲を明示選択する]
    #   処理: [scope/goalと任意profileを短いmemory交換で切り替え、旧frame仮説を破棄する]
    #   引数: [scope: host選択の完全範囲, goal_id: opaque目標参照, profile: 任意の同時選択方針]
    #   戻り値: [なし] エラー: [ValueError: 型/参照不正]
    # }
    def select_context(
        self,
        scope: KnowledgeScope | None,
        goal_id: str,
        profile: EvaluationProfile | None = None,
    ) -> None:
        if (
            scope is not None
            and type(scope) is not KnowledgeScope
            or profile is not None
            and type(profile) is not EvaluationProfile
        ):
            raise ValueError(
                "evaluation context must come from typed host configuration"
            )
        validate_identifier(goal_id, "runtime evaluation goal")
        with self._lock:
            self._scope, self._goal_id = scope, goal_id
            if profile is not None:
                self._profile = profile
            self._predictions = None

    # {
    #   責務: [apply: 候補許可・既存評価を維持して一stepの6軸projectionを付加する]
    #   処理: [方針/仮説/scopeを一度captureし、例外時も未知6軸で静的判断へ戻す]
    #   引数: [context/observation: 同じ判断材料, actual: receipt付き前操作結果,
    #     action_generation: 現SessionのStop/rearm世代]
    #   戻り値: [DecisionContext: 追加の評価projectionだけを付けた不変値]
    # }
    def apply(
        self,
        context: DecisionContext,
        observation: ScreenObservation,
        actual: ExecutionObservation | None = None,
        action_generation: int | None = None,
    ) -> DecisionContext:
        with self._lock:
            profile, predictions, scope, goal = (
                self._profile,
                self._predictions,
                self._scope,
                self._goal_id,
            )
            previous_binding = (
                self._bindings.get(actual.receipt.snapshot_id)
                if actual is not None
                else None
            )
            binding = (scope.key if scope else None, goal)
            matched = profile.matches(scope, goal)
            selected = profile if matched else EvaluationProfile()
            self._bindings[context.snapshot_id] = (*binding, selected)
            while len(self._bindings) > 32:
                self._bindings.popitem(last=False)
        try:
            actual_profile = (
                previous_binding[2] if previous_binding is not None else selected
            )
            previous = _actual_vector(
                actual,
                action_generation,
                previous_binding is not None and previous_binding[:2] == binding,
                actual_profile,
            )
            previous["profile"] = actual_profile.to_dict()
            prediction_map = {}
            reason = "no_candidate_predictions"
            if predictions is not None and matched:
                if (
                    scope is not None
                    and predictions.scope == scope
                    and predictions.goal_id == goal
                    and predictions.observation_ref
                    == evaluation_observation_reference(observation)
                ):
                    prediction_map = {
                        item.action_id: item for item in predictions.candidates
                    }
                    reason = "frame_scope_verified_hypothesis"
                else:
                    reason = "prediction_reference_mismatch"
            vectors = {}
            for candidate in context.candidates[:64]:
                if len(candidate.action_id) > 128:
                    continue
                predicted = prediction_map.get(candidate.action_id)
                vector = evaluate_runtime_vector(
                    predicted.axes if predicted else (),
                    selected,
                    kind="candidate_hypothetical",
                    status=reason if predicted else "no_candidate_evidence",
                )
                vector["reference"] = {
                    "prediction_version": predictions.version
                    if predicted and predictions
                    else None,
                    "observation_ref": predictions.observation_ref
                    if predicted and predictions
                    else None,
                }
                vectors[candidate.action_id] = vector
            value: dict[str, Any] = {
                "schema": "runtime-evaluation/v1",
                "context_snapshot_id": context.snapshot_id,
                "status": "applied" if matched else "fallback",
                "reason": reason if matched else "profile_scope_mismatch",
                "scope": scope.to_dict() if scope else None,
                "goal_id": goal,
                "profile": selected.to_dict(),
                "previous_actual": previous,
                "candidate_hypothetical": vectors,
                "truncated_candidates": max(0, len(context.candidates) - len(vectors)),
            }
            projection = RuntimeEvaluationProjection.from_dict(
                value, expose_to_models=self._expose_to_models
            )
        except Exception:
            value = {
                "schema": "runtime-evaluation/v1",
                "context_snapshot_id": context.snapshot_id,
                "status": "fallback",
                "reason": "evaluation_unavailable",
                "scope": None,
                "goal_id": "unresolved",
                "profile": EvaluationProfile().to_dict(),
                "previous_actual": evaluate_runtime_vector(
                    (),
                    EvaluationProfile(),
                    kind="previous_actual",
                    status="unavailable",
                ),
                "candidate_hypothetical": {},
                "truncated_candidates": len(context.candidates),
            }
            value["previous_actual"]["profile"] = EvaluationProfile().to_dict()
            projection = RuntimeEvaluationProjection.from_dict(value)
        return replace(context, evaluation_projection=projection)

    # {
    #   責務: [actual_evaluation: 次判断を必要とせず実操作後の値を元判断の方針で評価する]
    #   処理: [terminal/比較測定用に元snapshotのpolicyをcaptureし、候補仮説を参照しない]
    #   引数: [actual: receiptと観測結果, action_generation: 有効な停止世代]
    #   戻り値: [dict: 元policy/provenanceとactual専用6軸。履歴や候補を更新しない]
    # }
    def actual_evaluation(
        self, actual: ExecutionObservation, action_generation: int
    ) -> dict[str, Any]:
        with self._lock:
            binding = self._bindings.get(actual.receipt.snapshot_id)
            expected = (self._scope.key if self._scope else None, self._goal_id)
            bound = binding is not None and binding[:2] == expected
            profile = binding[2] if binding is not None else EvaluationProfile()
        vector = _actual_vector(actual, action_generation, bound, profile)
        vector["profile"] = profile.to_dict()
        return {"profile": profile.to_dict(), "evaluation": vector}


# {
#   責務: [_actual_vector: 同Session/scope/世代の正常実送信に対応した前回結果だけを評価する]
#   処理: [既知core detectorの観測根拠を読み、semantic Model自己申告やpixel差を進捗へ変換しない]
#   引数: [actual: 不変のreceipt/Outcome対, generation: 現世代, bound: 元判断scope一致,
#     profile: 元判断に束縛された価値基準]
#   戻り値: [dict: actual専用6軸。候補hypotheticalへは渡さない]
# }
def _actual_vector(
    actual: ExecutionObservation | None,
    generation: int | None,
    bound: bool,
    profile: EvaluationProfile,
) -> dict[str, Any]:
    if actual is None or not bound or generation is None:
        return evaluate_runtime_vector(
            (), profile, kind="previous_actual", status="no_bound_actual_observation"
        )
    receipt, outcome = actual.receipt, actual.outcome
    if (
        receipt.execution_status != "executed"
        or receipt.input_dispatch_started is not True
        or not receipt.eligible_for_learning
        or receipt.action_generation != generation
    ):
        return evaluate_runtime_vector(
            (), profile, kind="previous_actual", status="receipt_not_current_executed"
        )
    axes: dict[str, AxisPrediction] = {}
    cues = []
    for item in outcome.evidence[:32]:
        if item.detector in {"screen_diff", "state_delta"} and item.value in {
            "changed",
            "stable",
            "unknown",
        }:
            cues.append(
                {
                    "source": item.detector,
                    "classification": item.value,
                    "meaning": "perceptual_change_not_semantic_progress",
                }
            )
        if (
            item.detector == "state_delta"
            and item.signal == "state_delta"
            and item.provenance == "state_delta/v1"
            and item.details.get("state_evidence_available") is True
        ):
            confidence = item.confidence * item.reliability
            for axis_name, field in (
                ("progress", "progress"),
                ("resources", "resources"),
            ):
                changes = item.details.get("changed_fields", {})
                change = changes.get(field) if type(changes) is dict else None
                if type(change) is dict:
                    before, after = change.get("before"), change.get("after")
                    if (
                        type(before) in (int, float)
                        and type(after) in (int, float)
                        and isfinite(cast(float, before))
                        and isfinite(cast(float, after))
                    ):
                        axes[axis_name] = AxisPrediction(
                            axis_name,
                            max(
                                -1.0, min(1.0, cast(float, after) - cast(float, before))
                            ),
                            confidence,
                            "observed_state_metric",
                            "1",
                        )
            if item.value in {"changed", "stable"}:
                axes["novelty"] = AxisPrediction(
                    "novelty",
                    float(item.value == "changed"),
                    confidence,
                    "observed_state_cue",
                    "1",
                )
                axes["repetition"] = AxisPrediction(
                    "repetition",
                    -float(item.value == "stable"),
                    confidence,
                    "observed_state_cue",
                    "1",
                )
        if (
            not outcome.conflict
            and not outcome.abstained
            and outcome.status in {"success", "failure"}
            and item.detector == "terminal_text"
            and item.signal == "terminal"
            and item.provenance == "terminal_text/v1"
            and item.value == outcome.status
        ):
            value = 1.0 if outcome.status == "success" else -1.0
            for axis_name in ("progress", "survival"):
                axes[axis_name] = AxisPrediction(
                    axis_name,
                    value,
                    item.confidence * item.reliability,
                    "observed_terminal_pattern",
                    "1",
                )
    vector = evaluate_runtime_vector(
        tuple(axes.values()),
        profile,
        kind="previous_actual",
        status="observed_evidence" if axes else "no_supported_actual_axis",
    )
    vector["perceptual_cues"] = cues[:8]
    vector["reference"] = {
        "snapshot_id": receipt.snapshot_id,
        "action_ref": "action." + sha256(receipt.action_id.encode()).hexdigest(),
        "action_generation": receipt.action_generation,
        "after_screen_ref": "screen."
        + sha256(actual.after_screen_id.encode()).hexdigest(),
        "after_state_ref": "state."
        + sha256(actual.after_signature.encode()).hexdigest(),
    }
    return vector
