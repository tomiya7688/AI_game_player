"""#173 Candidate Evaluation Runner: ChampionとChallengerを同じ症例で採点し、改善・回帰・latency/資源を分けて返す。

採否は行わない（#171の`ChampionRegistry`）。返す`CandidateEvaluation`は#171の入力になり、`EvaluationEvent`は保存できる。
採点は確定ラベル(#647 verified_effect)だけを正解として使い、offline検証であり実世界検証の代替ではない。
"""

from collections.abc import Callable, Sequence
import json
import math
import time
from typing import Any

from ai_game_player.artifact_store import ArtifactReference, FileSystemArtifactStore
from ai_game_player.challenger_evaluation_models import (
    GROUPS,
    ArmSummary,
    EvaluationCase,
    EvaluationError,
    EvaluationEvent,
    EvaluationPolicy,
)
from ai_game_player.challenger_promotion_models import CandidateEvaluation
from ai_game_player.challenger_update_models import ChallengerArtifact, ComponentParameters, canonical

MAX_EVENT_BYTES = 256 * 1024
EVENT_ARTIFACT_TYPE = "learning.evaluation_event"
SIGNAL_FIELDS = {"decision": "decision_expected_success", "recognition": "recognition_reliability"}


# {
#   責務: [predict_accept: parameterで較正した信号が閾値以上なら採用(True)と判定する]
#   処理: [componentに対応する信号(decision→予測成功率, recognition→認識信頼度)にconfidence_scaleを掛けて1.0で頭打ちにし、acceptance_thresholdと比べる]
#   引数: [parameters: 評価するparameter, case: #169の診断症例] 戻り値: [bool: 採用するか]
#   エラー: [ValueError: 信号が無い症例]
# }
def predict_accept(parameters: ComponentParameters, case: Any) -> bool:
    signal = getattr(case, SIGNAL_FIELDS[parameters.component])
    if signal is None:
        raise ValueError("case has no signal for this component")
    return min(signal * parameters.confidence_scale, 1.0) >= parameters.acceptance_threshold


# {
#   責務: [evaluate_candidate: 同じ症例でChampionとChallengerを採点し、#171向けの評価と保存用Eventを返す]
#   処理: [1: 入力(artifact型・component・scope・群・重複)を検査, 2: 各症例を両armで採点(確定ラベルが無い症例は採点対象外),
#     3: 例外・時間超過は誤り扱いで母数に含める, 4: 群×armで集計, 5: 改善・回帰・資源から学習Level信号を決める]
#   引数: [artifact: #170のChallenger, cases: 群つき症例, policy: 方針, evaluation_ref/evaluated_at: 識別子と測り終えた時刻,
#     predictor: 採点関数(試験用に差替え可), clock: 秒を返す時計(試験用に差替え可)]
#   戻り値: [tuple[CandidateEvaluation, EvaluationEvent]]
#   エラー: [EvaluationError: invalid_case/scope_mismatch/duplicate_case/invalid_artifact]
# }
def evaluate_candidate(
    artifact: ChallengerArtifact,
    cases: Sequence[EvaluationCase],
    policy: EvaluationPolicy,
    *,
    evaluation_ref: str,
    evaluated_at: str,
    predictor: Callable[[ComponentParameters, Any], bool] = predict_accept,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[CandidateEvaluation, EvaluationEvent]:
    if type(artifact) is not ChallengerArtifact or type(policy) is not EvaluationPolicy:
        raise EvaluationError("invalid_artifact", "artifact and policy must be typed values")
    if artifact.parent.component not in SIGNAL_FIELDS:
        raise EvaluationError("invalid_artifact", "this component has no evaluable signal")
    _check_cases(artifact, cases)
    arms = {"champion": artifact.parent, "challenger": artifact.challenger}
    summaries = {
        group: {
            name: _summarize(parameters, [c.case for c in cases if c.group == group], policy, predictor, clock)
            for name, parameters in arms.items()
        }
        for group in GROUPS
    }
    signal, reasons = _signal(summaries, policy)
    event = EvaluationEvent(
        evaluation_ref=evaluation_ref,
        evaluated_at=evaluated_at,
        validation_state="offline",
        artifact_id=artifact.artifact_id,
        champion_sha256=artifact.parent.sha256,
        challenger_sha256=artifact.challenger.sha256,
        policy_fingerprint=policy.fingerprint,
        groups=summaries,
        resource_cost=artifact.resource_cost.to_dict(),
        signal=signal,
        reasons=reasons,
    )
    target, regression = summaries["target"], summaries["regression"]
    evaluation = CandidateEvaluation(
        evaluation_ref=evaluation_ref,
        evaluated_at=evaluated_at,
        artifact_id=artifact.artifact_id,
        champion_sha256=artifact.parent.sha256,
        challenger_sha256=artifact.challenger.sha256,
        target_cases=min(target["champion"].scored, target["challenger"].scored),
        regression_cases=min(regression["champion"].scored, regression["challenger"].scored),
        target_baseline=target["champion"].accuracy,
        target_candidate=target["challenger"].accuracy,
        regression_baseline=regression["champion"].accuracy,
        regression_candidate=regression["challenger"].accuracy,
    )
    return evaluation, event


# {
#   責務: [save_evaluation_event: 評価Eventを内容hashの不変objectとして保存する]
#   処理: [正規化JSONを上限内で保存する] 引数: [store: 保存先, event: 評価Event] 戻り値: [ArtifactReference]
#   エラー: [EvaluationError: invalid_event(型不正・上限超過)]
# }
def save_evaluation_event(store: FileSystemArtifactStore, event: EvaluationEvent) -> ArtifactReference:
    if type(event) is not EvaluationEvent:
        raise EvaluationError("invalid_event", "event type is invalid")
    payload = canonical(event.to_dict())
    if len(payload) > MAX_EVENT_BYTES:
        raise EvaluationError("invalid_event", "event is too large")
    return store.put(payload, media_type="application/json", artifact_type=EVENT_ARTIFACT_TYPE, sensitive=False)


# {
#   責務: [load_evaluation_event: 保存Eventを上限付きで読み、重複key・NaN・内容改ざんを拒否して復元する]
#   処理: [hash検証付きで読み、strictにJSONを解析し、from_dictでevent_idを再計算して照合する]
#   引数: [store: 保存先, reference: save_evaluation_eventの参照] 戻り値: [EvaluationEvent]
#   エラー: [EvaluationError: invalid_event, OSError: 読取失敗]
# }
def load_evaluation_event(store: FileSystemArtifactStore, reference: ArtifactReference) -> EvaluationEvent:
    try:
        payload = store.read_bounded(reference, max_bytes=MAX_EVENT_BYTES, expected_artifact_type=EVENT_ARTIFACT_TYPE)
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as error:
        raise EvaluationError("invalid_event", str(error)) from error
    return EvaluationEvent.from_dict(value)


def _check_cases(artifact: ChallengerArtifact, cases: Sequence[EvaluationCase]) -> None:
    seen = set()
    for item in cases:
        if type(item) is not EvaluationCase:
            raise EvaluationError("invalid_case", "cases must be EvaluationCase values")
        if item.case.scope != artifact.parent.scope:
            raise EvaluationError("scope_mismatch", "case scope differs from the artifact scope")
        key = (item.group, item.case.ref)
        if key in seen:
            raise EvaluationError("duplicate_case", "the same case appears twice in one group")
        seen.add(key)


def _summarize(
    parameters: ComponentParameters,
    cases: list[Any],
    policy: EvaluationPolicy,
    predictor: Callable[[ComponentParameters, Any], bool],
    clock: Callable[[], float],
) -> ArmSummary:
    scored = correct = failed = unscorable = 0
    latencies: list[float] = []
    for case in cases:
        label = _label(case)
        if label is None:
            unscorable += 1
            continue
        scored += 1
        started = clock()
        try:
            accepted = predictor(parameters, case)
        except (ValueError, TypeError, ArithmeticError):
            failed += 1
            latencies.append((clock() - started) * 1000)
            continue
        elapsed_ms = (clock() - started) * 1000
        latencies.append(elapsed_ms)
        if elapsed_ms > policy.case_latency_budget_ms:
            failed += 1  # 時間超過は誤り扱いにして母数へ含める
        elif accepted == label:
            correct += 1
    return ArmSummary(scored, correct, failed, unscorable, correct / scored if scored else 0.0, _p95(latencies))


def _label(case: Any) -> bool | None:
    """確定ラベル(verified_effect)で成功/失敗が決まる症例だけ、成功ならTrueを返す。"""
    if case.label_status != "verified_effect" or case.outcome_conflict or case.outcome_abstained:
        return None
    if case.outcome == "success":
        return True
    if case.outcome == "failure":
        return False
    return None


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)], 6)


def _signal(summaries: dict[str, dict[str, ArmSummary]], policy: EvaluationPolicy) -> tuple[str, tuple[str, ...]]:
    target, regression = summaries["target"], summaries["regression"]
    scored = min(
        target["champion"].scored, target["challenger"].scored,
        regression["champion"].scored, regression["challenger"].scored,
    )
    drop = round(regression["champion"].accuracy - regression["challenger"].accuracy, 9)
    gain = round(target["challenger"].accuracy - target["champion"].accuracy, 9)
    if drop > policy.regression_tolerance:
        return "de_escalate", ("regression",)
    if scored < policy.min_scored_per_group:
        return "hold", ("insufficient_evidence",)
    if target["challenger"].failed > target["champion"].failed:
        return "de_escalate", ("more_failures",)
    if gain >= policy.min_gain:
        return "escalate", ("improved_without_regression",)
    return "hold", ("no_significant_change",)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in evaluation event")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite number in evaluation event: {value}")
