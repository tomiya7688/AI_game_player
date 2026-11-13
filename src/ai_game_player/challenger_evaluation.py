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
    PredictionInput,
)
from ai_game_player.challenger_promotion_models import CandidateEvaluation
from ai_game_player.challenger_update_models import (
    ChallengerArtifact,
    ComponentParameters,
    canonical,
)

MAX_EVENT_BYTES = 256 * 1024
EVENT_ARTIFACT_TYPE = "learning.evaluation_event"
SIGNAL_FIELDS = {
    "decision": "decision_expected_success",
    "recognition": "recognition_reliability",
}
# 組込みの採点関数(predict_accept)が評価できるstrategy。prototype(負例参照の追加)はscale/thresholdを変えず、
# predict_acceptはprototypesを参照しないため、prototypeだけ違うChallengerはChampionと必ず同じ予測になる。
# それを「改善0」と測らず、評価不能として昇格を保留する。prototypeを使う採点関数を渡す時は、呼出側が"prototype"を含めて指定する。
BUILTIN_EVALUABLE_STRATEGIES = ("calibration", "threshold")
UNSUPPORTED_EVALUATION = "unsupported_evaluation"


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
    return (
        min(signal * parameters.confidence_scale, 1.0)
        >= parameters.acceptance_threshold
    )


# {
#   責務: [evaluate_candidate: 同じ症例でChampionとChallengerを採点し、#171向けの評価と保存用Eventを返す]
#   処理: [1: 入力(artifact型・component・scope・群・重複)を検査, 2: 各症例を両armで採点(確定ラベルが無い症例は採点対象外),
#     3: 例外(型を問わずException)・時間超過は誤り扱いで母数に含める(同期callableは途中で強制停止できず、wall-clock上限は呼出側のworker/processで強制する), 4: 群×armで集計, 5: 改善・回帰・資源から学習Level信号を決める]
#   引数: [artifact: #170のChallenger, cases: 群つき症例, policy: 方針, evaluation_ref/evaluated_at: 識別子と測り終えた時刻,
#     predictor: 採点関数(試験用に差替え可。予測用の入力PredictionInputだけを受け取り、正解ラベルは渡されない), clock: 秒を返す時計(試験用に差替え可),
#     evaluable_strategies: predictorが評価できるstrategy。既定のpredict_acceptはprototypeを評価できないので、prototypeだけ違う候補は採点不可(unsupported_evaluation)として0件・holdで返す]
#   戻り値: [tuple[CandidateEvaluation, EvaluationEvent]]
#   エラー: [EvaluationError: invalid_case/scope_mismatch/duplicate_case(同一refの重複。群をまたぐものも含む)/invalid_artifact]
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
    evaluable_strategies: Sequence[str] = BUILTIN_EVALUABLE_STRATEGIES,
) -> tuple[CandidateEvaluation, EvaluationEvent]:
    if type(artifact) is not ChallengerArtifact or type(policy) is not EvaluationPolicy:
        raise EvaluationError(
            "invalid_artifact", "artifact and policy must be typed values"
        )
    if artifact.parent.component not in SIGNAL_FIELDS:
        raise EvaluationError(
            "invalid_artifact", "this component has no evaluable signal"
        )
    _check_cases(artifact, cases)
    if not _is_evaluable(artifact, evaluable_strategies):
        return _unsupported(
            artifact,
            cases,
            policy,
            evaluation_ref=evaluation_ref,
            evaluated_at=evaluated_at,
        )
    arms = {"champion": artifact.parent, "challenger": artifact.challenger}
    summaries = {
        group: {
            name: _summarize(
                parameters,
                [c.case for c in cases if c.group == group],
                policy,
                predictor,
                clock,
            )
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
        # #171 Gateの型は故障数を持たない。故障が増えた候補は回帰証拠を渡さず(0件)、Gateに insufficient_regression_cases で拒否させる。
        regression_cases=(
            0
            if _failures_increased(summaries)
            else min(regression["champion"].scored, regression["challenger"].scored)
        ),
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
def save_evaluation_event(
    store: FileSystemArtifactStore, event: EvaluationEvent
) -> ArtifactReference:
    if type(event) is not EvaluationEvent:
        raise EvaluationError("invalid_event", "event type is invalid")
    payload = canonical(event.to_dict())
    if len(payload) > MAX_EVENT_BYTES:
        raise EvaluationError("invalid_event", "event is too large")
    return store.put(
        payload,
        media_type="application/json",
        artifact_type=EVENT_ARTIFACT_TYPE,
        sensitive=False,
    )


# {
#   責務: [load_evaluation_event: 保存Eventを上限付きで読み、重複key・NaN・内容改ざんを拒否して復元する]
#   処理: [hash検証付きで読み、strictにJSONを解析し、from_dictでevent_idを再計算して照合する]
#   引数: [store: 保存先, reference: save_evaluation_eventの参照] 戻り値: [EvaluationEvent]
#   エラー: [EvaluationError: invalid_event, OSError: 読取失敗]
# }
def load_evaluation_event(
    store: FileSystemArtifactStore, reference: ArtifactReference
) -> EvaluationEvent:
    try:
        payload = store.read_bounded(
            reference,
            max_bytes=MAX_EVENT_BYTES,
            expected_artifact_type=EVENT_ARTIFACT_TYPE,
        )
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise EvaluationError("invalid_event", str(error)) from error
    return EvaluationEvent.from_dict(value)


def _check_cases(artifact: ChallengerArtifact, cases: Sequence[EvaluationCase]) -> None:
    seen = set()
    for item in cases:
        if type(item) is not EvaluationCase:
            raise EvaluationError("invalid_case", "cases must be EvaluationCase values")
        if item.case.scope != artifact.parent.scope:
            raise EvaluationError(
                "scope_mismatch", "case scope differs from the artifact scope"
            )
        # targetとregressionは独立した改善/回帰検査なので、同じ症例をどちらにも置かせない(検証データ漏洩と件数の二重計上を防ぐ)。
        if item.case.ref in seen:
            raise EvaluationError(
                "duplicate_case",
                "the same case appears twice (in one group or across groups)",
            )
        seen.add(item.case.ref)


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
            accepted = predictor(parameters, _prediction_input(case))
        except Exception:  # noqa: BLE001  # 型を問わず誤り扱いで母数へ残す。KeyboardInterrupt/SystemExitはBaseExceptionなので握り潰さない。
            failed += 1
            latencies.append((clock() - started) * 1000)
            continue
        elapsed_ms = (clock() - started) * 1000
        latencies.append(elapsed_ms)
        if elapsed_ms > policy.case_latency_budget_ms:
            failed += 1  # 時間超過は誤り扱いにして母数へ含める
        elif accepted == label:
            correct += 1
    return ArmSummary(
        scored,
        correct,
        failed,
        unscorable,
        correct / scored if scored else 0.0,
        _p95(latencies),
    )


# {
#   責務: [_prediction_input: 症例から予測に使う信号だけを取り出し、正解ラベルを渡さない入力にする]
#   処理: [ref・decision/recognitionの信号だけを複写する] 引数: [case: #169の診断症例] 戻り値: [PredictionInput]
# }
def _prediction_input(case: Any) -> PredictionInput:
    return PredictionInput(
        ref=case.ref,
        decision_expected_success=case.decision_expected_success,
        recognition_reliability=case.recognition_reliability,
    )


# {
#   責務: [_is_evaluable: 候補のstrategyと変更点を、採点関数が評価できるか判定する]
#   処理: [strategyが対象外、またはprototypeが親と違うのにprototypeが対象外なら評価不能とする]
#   引数: [artifact: Challenger, evaluable_strategies: 採点関数が評価できるstrategy] 戻り値: [bool]
# }
def _is_evaluable(
    artifact: ChallengerArtifact, evaluable_strategies: Sequence[str]
) -> bool:
    if artifact.proposal.strategy not in evaluable_strategies:
        return False
    changes_prototypes = artifact.challenger.prototypes != artifact.parent.prototypes
    return "prototype" in evaluable_strategies or not changes_prototypes


# {
#   責務: [_unsupported: 評価不能な候補を、改善0と測らず採点不可(0件・hold)として返す]
#   処理: [全症例をunscorableとして数え、信号はhold・理由はunsupported_evaluation、#171向けの評価は0件にして昇格させない]
#   引数: [artifact/cases/policy: 評価の入力, evaluation_ref/evaluated_at: 識別子と時刻] 戻り値: [tuple[CandidateEvaluation, EvaluationEvent]]
# }
def _unsupported(
    artifact: ChallengerArtifact,
    cases: Sequence[EvaluationCase],
    policy: EvaluationPolicy,
    *,
    evaluation_ref: str,
    evaluated_at: str,
) -> tuple[CandidateEvaluation, EvaluationEvent]:
    summaries = {
        group: {
            name: ArmSummary(
                0, 0, 0, sum(1 for c in cases if c.group == group), 0.0, 0.0
            )
            for name in ("champion", "challenger")
        }
        for group in GROUPS
    }
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
        signal="hold",
        reasons=(UNSUPPORTED_EVALUATION,),
    )
    evaluation = CandidateEvaluation(
        evaluation_ref=evaluation_ref,
        evaluated_at=evaluated_at,
        artifact_id=artifact.artifact_id,
        champion_sha256=artifact.parent.sha256,
        challenger_sha256=artifact.challenger.sha256,
        target_cases=0,
        regression_cases=0,
        target_baseline=0.0,
        target_candidate=0.0,
        regression_baseline=0.0,
        regression_candidate=0.0,
    )
    return evaluation, event


def _label(case: Any) -> bool | None:
    """確定ラベル(verified_effect)で成功/失敗が決まる症例だけ、成功ならTrueを返す。"""
    if (
        case.label_status != "verified_effect"
        or case.outcome_conflict
        or case.outcome_abstained
    ):
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


# {
#   責務: [challengerのtarget/regression群で故障(例外・時間超過)がchampionより増えたかを判定する]
#   処理: [各群のfailedを比較する] 引数: [summaries: 群別のArm集計] 戻り値: [増えたらTrue]
# }
def _failures_increased(summaries: dict[str, dict[str, ArmSummary]]) -> bool:
    return any(
        summaries[group]["challenger"].failed > summaries[group]["champion"].failed
        for group in GROUPS
    )


def _signal(
    summaries: dict[str, dict[str, ArmSummary]], policy: EvaluationPolicy
) -> tuple[str, tuple[str, ...]]:
    target, regression = summaries["target"], summaries["regression"]
    scored = min(
        target["champion"].scored,
        target["challenger"].scored,
        regression["champion"].scored,
        regression["challenger"].scored,
    )
    drop = round(regression["champion"].accuracy - regression["challenger"].accuracy, 9)
    if regression["challenger"].failed > regression["champion"].failed:
        # 元から不正解の回帰症例がTimeout/例外へ置換されてもaccuracyは変わらないため、故障数の増加を回帰として扱う。
        return "de_escalate", ("regression_failures",)
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
