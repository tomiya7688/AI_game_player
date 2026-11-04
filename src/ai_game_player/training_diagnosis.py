"""ExperienceのTraining Valueを付与し、誤りの原因候補componentを決定論的に選別する。

#169の診断だけを行い、モデル更新(#170)・採否(#171)・評価(#173)は行わない。
確定ラベルの判定は#647の段階(verified_effectだけが確定)に従い、不確実な根拠は
#712のsoft学習へ渡せる仮説(hypothesis)として確信度上限付きで残す。
"""

from collections.abc import Iterable, Mapping
from math import prod
from typing import Any

from ai_game_player.experience import ExperienceEpisode, ExperienceEvent
from ai_game_player.knowledge_update_sample import (
    OUTCOME_EVENT_TYPE,
    KnowledgeUpdateSample,
)
from ai_game_player.learning_dataset import DatasetSource
from ai_game_player.training_diagnosis_models import (
    COMPONENTS,
    CaseDiagnosis,
    ComponentAttribution,
    ComponentTarget,
    DiagnosisCase,
    TrainingDiagnosisReport,
)
from ai_game_player.training_diagnosis_policy import (
    LABEL_STATUSES,
    TrainingDiagnosisPolicy,
    weaker_label_status,
)


CONTEXT_EVENT_TYPE = "learning.diagnosis_context"
CONTEXT_FIELDS = frozenset(
    {
        "outcome_event_id",
        "label_status",
        "recognition_reliability",
        "decision_expected_success",
        "outcome_conflict",
        "outcome_abstained",
    }
)
_NOT_EXECUTED = frozenset({"dry_run", "planned", "cancelled"})
# 既存の`verified`は信頼済み統合境界の証拠だが、CALIBRATEDは#647で確定ラベルにならない。
_STRICT_EVIDENCE = frozenset({"deterministic_action_effect", "human_correction"})
_SUPPORTED_EVIDENCE = frozenset({"calibrated_action_effect"})
_EXECUTION_FAILURE_CONFIDENCE = 0.9
_EXECUTION_UNCONFIRMED_CONFIDENCE = 0.5
_CONFLICT_CONFIDENCE = 0.8
_ABSTAINED_CONFIDENCE = 0.3
_UNKNOWN_OUTCOME_CONFIDENCE = 0.1
_INEFFECTIVE_DECISION_SHARE = 0.5
_OUTCOME_STRENGTH = {"failure": 1.0, "no_effect": 0.7}


# {
#   責務: [diagnose_episodes: Experience episode群からcaseを抽出し、診断報告を作る]
#   処理: [episode_id順・step順にcaseを集め、除外理由を数えてdiagnose_casesへ渡す]
#   引数: [episodes: canonical ExperienceEpisode群, policy: 重み・上限を固定する診断policy]
#   戻り値: [TrainingDiagnosisReport: 入力順に依存しない版付き報告]
#   エラー: [ValueError: episode以外の値]
# }
def diagnose_episodes(
    episodes: Iterable[ExperienceEpisode],
    policy: TrainingDiagnosisPolicy | None = None,
) -> TrainingDiagnosisReport:
    policy = policy or TrainingDiagnosisPolicy()
    cases, excluded = collect_diagnosis_cases(episodes, policy)
    return diagnose_cases(cases, policy, excluded)


# {
#   責務: [collect_diagnosis_cases: Experienceの操作効果eventと任意の診断文脈eventをcaseへ変換する]
#   処理: [knowledge.action_outcomeを既存KnowledgeUpdateSampleで検証し、同じstepの文脈eventを
#     outcome_event_idで結び付ける。未実行・不正・重複・文脈不整合・上限超過は除外理由として数える]
#   引数: [episodes: ExperienceEpisode群, policy: max_casesを持つ診断policy]
#   戻り値: [tuple[tuple[DiagnosisCase, ...], dict[str, int]]: 参照順のcaseと除外理由ごとの件数]
#   エラー: [ValueError: episode以外の値]
# }
def collect_diagnosis_cases(
    episodes: Iterable[ExperienceEpisode], policy: TrainingDiagnosisPolicy
) -> tuple[tuple[DiagnosisCase, ...], dict[str, int]]:
    ordered = list(episodes)
    if any(not isinstance(episode, ExperienceEpisode) for episode in ordered):
        raise ValueError("diagnosis requires canonical experience episodes")
    cases: list[DiagnosisCase] = []
    excluded: dict[str, int] = {}
    seen: set[DatasetSource] = set()
    for episode in sorted(ordered, key=lambda item: item.episode_id):
        for step in sorted(
            episode.steps, key=lambda item: (item.sequence, item.step_id)
        ):
            contexts, invalid_contexts = _contexts_by_outcome(step.events)
            _count(excluded, "invalid_context", invalid_contexts)
            matched: set[str] = set()
            for event in step.events:
                if event.event_type != OUTCOME_EVENT_TYPE:
                    continue
                ref = DatasetSource(episode.episode_id, step.step_id, event.event_id)
                context = contexts.get(event.event_id)
                matched.add(event.event_id)
                reason, case = _case(ref, event, context)
                if reason is None and ref in seen:
                    reason = "duplicate_reference"
                if reason is None and len(cases) >= policy.max_cases:
                    reason = "case_limit"
                if reason is not None:
                    _count(excluded, reason, 1)
                    continue
                seen.add(ref)
                cases.append(case)
            _count(excluded, "orphan_context", len(set(contexts) - matched))
    return tuple(cases), excluded


# {
#   責務: [diagnose_cases: case群をcase診断とscope別component候補へ集計する]
#   処理: [各caseを診断し、training value上位とcomponent×scopeの結合確信度を上限内で並べる。
#     確定根拠がminimum_strict_support未満の候補はhypothesisとし確信度を上限で抑える]
#   引数: [cases: 診断case群, policy: 診断policy, excluded: 抽出時の除外理由ごとの件数]
#   戻り値: [TrainingDiagnosisReport: 決定論的な版付き報告]
#   エラー: [ValueError: case以外の値、またはmax_casesを超える件数]
# }
def diagnose_cases(
    cases: Iterable[DiagnosisCase],
    policy: TrainingDiagnosisPolicy,
    excluded: Mapping[str, int] | None = None,
) -> TrainingDiagnosisReport:
    received = list(cases)
    if any(not isinstance(case, DiagnosisCase) for case in received):
        raise ValueError("diagnosis requires DiagnosisCase values")
    ordered = sorted(received, key=lambda case: case.ref)
    if len(ordered) > policy.max_cases:
        raise ValueError("diagnosis cases exceed the policy limit")
    diagnoses = [diagnose_case(case, policy) for case in ordered]
    findings = sorted(
        (item for item in diagnoses if item.training_value > 0),
        key=lambda item: (-item.training_value, item.ref),
    )
    targets = _targets(diagnoses, policy)
    return TrainingDiagnosisReport(
        policy_fingerprint=policy.fingerprint,
        cases_considered=len(ordered),
        excluded=tuple(sorted((excluded or {}).items())),
        findings=tuple(findings[: policy.max_findings]),
        findings_truncated=len(findings) > policy.max_findings,
        targets=tuple(targets[: policy.max_targets]),
        targets_truncated=len(targets) > policy.max_targets,
    )


# {
#   責務: [diagnose_case: 一つのcaseの誤り・低価値をcomponentへ帰属し、training valueを計算する]
#   処理: [実行失敗はreceipt事実としてexecutionへ、検出器の矛盾・保留はoutcome_interpretationへ、
#     failure/no_effectは認識信頼度と判断時予測からrecognition/decisionへ配分する。確定ラベル以外は
#     strict=Falseかつuncertain_confidence_cap以下に抑え、training valueは帰属確信度の結合値にする]
#   引数: [case: 診断case, policy: 診断policy]
#   戻り値: [CaseDiagnosis: 帰属を確信度降順に並べたcase診断]
# }
def diagnose_case(
    case: DiagnosisCase, policy: TrainingDiagnosisPolicy
) -> CaseDiagnosis:
    attributions = _attributions(case, policy)
    attributions = [item for item in attributions if item.confidence > 0]
    attributions.sort(
        key=lambda item: (
            -item.confidence,
            COMPONENTS.index(item.component),
            item.failure_kind,
        )
    )
    value = round(1 - prod(1 - item.confidence for item in attributions), 6)
    return CaseDiagnosis(
        case.ref, case.scope, case.label_status, value, tuple(attributions)
    )


# {
#   責務: [_attributions: caseの信号から帰属候補を列挙する]
#   処理: [実行状態、検出器の矛盾、効果分類の順に判定し、確定度で確信度を調整する]
#   引数: [case: 診断case, policy: 診断policy]
#   戻り値: [list[ComponentAttribution]: 確信度0を含み得る帰属候補]
# }
def _attributions(
    case: DiagnosisCase, policy: TrainingDiagnosisPolicy
) -> list[ComponentAttribution]:
    if case.execution_status == "before_dispatch_failure":
        return [
            ComponentAttribution(
                "execution",
                "execution_failure",
                _EXECUTION_FAILURE_CONFIDENCE,
                "receipt",
                True,
            )
        ]
    if case.execution_status == "dispatch_unknown":
        # 入力が届いたか不明なので、効果をrecognition/decisionへ帰属しない。
        confidence = min(
            _EXECUTION_UNCONFIRMED_CONFIDENCE, policy.uncertain_confidence_cap
        )
        return [
            ComponentAttribution(
                "execution", "execution_unconfirmed", confidence, "receipt", False
            )
        ]
    if case.outcome_conflict or case.label_status == "conflict":
        confidence = min(_CONFLICT_CONFIDENCE, policy.uncertain_confidence_cap)
        return [
            ComponentAttribution(
                "outcome_interpretation",
                "outcome_conflict",
                round(confidence, 6),
                "outcome_disagreement",
                False,
            )
        ]
    if case.outcome == "unknown" or case.outcome_abstained:
        base = (
            _ABSTAINED_CONFIDENCE
            if case.outcome_abstained
            else _UNKNOWN_OUTCOME_CONFIDENCE
        )
        return [
            ComponentAttribution(
                "outcome_interpretation",
                "outcome_unresolved",
                min(base, policy.uncertain_confidence_cap),
                "outcome_disagreement",
                False,
            )
        ]
    strict = case.label_status == "verified_effect"
    scale = policy.label_weight(case.label_status) * case.outcome_confidence
    reliability = case.recognition_reliability
    expected = case.decision_expected_success
    result: list[tuple[str, str, float]] = []
    if case.outcome in _OUTCOME_STRENGTH:
        strength = _OUTCOME_STRENGTH[case.outcome] * scale
        if reliability is not None and reliability < policy.low_reliability:
            result.append(
                ("recognition", "recognition_unreliable", strength * (1 - reliability))
            )
        # 認識が不確かなほど、選んだ判断そのものへの帰属を弱める。
        share = 1.0 if reliability is None else reliability
        if expected is not None and expected >= policy.overconfident_expectation:
            result.append(
                ("decision", "decision_overconfident", strength * expected * share)
            )
        else:
            result.append(
                (
                    "decision",
                    "action_ineffective",
                    strength * _INEFFECTIVE_DECISION_SHARE * share,
                )
            )
    elif expected is not None and expected <= policy.underestimated_expectation:
        result.append(
            (
                "decision",
                "decision_underestimated",
                scale * (1 - expected) * _INEFFECTIVE_DECISION_SHARE,
            )
        )
    basis = "verified_label" if strict else "uncertain_label"
    return [
        ComponentAttribution(
            component,
            kind,
            round(min(value, 1.0 if strict else policy.uncertain_confidence_cap), 6),
            basis,
            strict,
        )
        for component, kind, value in result
    ]


# {
#   責務: [_targets: case診断をcomponent×scopeごとの候補へ集計する]
#   処理: [確定根拠と不確実な根拠を別に数え、candidateは確定根拠だけの結合確信度、
#     hypothesisは全根拠の結合確信度を上限で抑えた値にする。参照は確信度上位だけ残す]
#   引数: [diagnoses: case診断群, policy: 診断policy]
#   戻り値: [list[ComponentTarget]: candidate優先・確信度降順の候補]
# }
def _targets(
    diagnoses: list[CaseDiagnosis], policy: TrainingDiagnosisPolicy
) -> list[ComponentTarget]:
    groups: dict[tuple[str, str], list[tuple[CaseDiagnosis, ComponentAttribution]]] = {}
    for diagnosis in diagnoses:
        for attribution in diagnosis.attributions:
            key = (attribution.component, diagnosis.scope.key)
            groups.setdefault(key, []).append((diagnosis, attribution))
    targets = []
    for (component, _scope_key), members in groups.items():
        strict = [item.confidence for _, item in members if item.strict]
        candidate = len(strict) >= policy.minimum_strict_support
        if candidate:
            confidence = 1 - prod(1 - value for value in strict)
        else:
            confidence = min(
                1 - prod(1 - item.confidence for _, item in members),
                policy.uncertain_confidence_cap,
            )
        kinds: dict[str, int] = {}
        for _, item in members:
            _count(kinds, item.failure_kind, 1)
        ranked = sorted(
            members,
            key=lambda pair: (not pair[1].strict, -pair[1].confidence, pair[0].ref),
        )
        targets.append(
            ComponentTarget(
                component=component,
                scope=members[0][0].scope,
                status="candidate" if candidate else "hypothesis",
                confidence=round(confidence, 6),
                strict_support=len(strict),
                uncertain_support=len(members) - len(strict),
                failure_kinds=tuple(sorted(kinds.items())),
                evidence_refs=tuple(
                    pair[0].ref for pair in ranked[: policy.max_evidence_refs]
                ),
            )
        )
    targets.sort(
        key=lambda item: (
            item.status != "candidate",
            -item.confidence,
            COMPONENTS.index(item.component),
            item.scope.key,
        )
    )
    return targets


# {
#   責務: [_case: 一つの操作効果eventと任意の文脈eventからcaseを作る]
#   処理: [既存KnowledgeUpdateSampleで検証し、未実行を除外し、確定度は既存証拠と文脈の弱い方にする]
#   引数: [ref: 元Experience参照, event: knowledge.action_outcome event,
#     context: 同じstepの検証済み文脈payload、または無し]
#   戻り値: [tuple[str | None, DiagnosisCase | None]: 除外理由とcase。どちらか一方だけが値を持つ]
# }
def _case(
    ref: DatasetSource, event: ExperienceEvent, context: Mapping[str, Any] | None
) -> tuple[str | None, DiagnosisCase | None]:
    try:
        sample = KnowledgeUpdateSample.from_event(event)
    except ValueError:
        return "invalid_outcome_event", None
    if sample.execution_status in _NOT_EXECUTED:
        return "not_executed", None
    label = _sample_label_status(sample)
    reliability = sample.candidate_reliability
    expected = None
    conflict = abstained = False
    if context is not None:
        label = weaker_label_status(label, context["label_status"])
        if context["recognition_reliability"] is not None:
            reliability = (
                context["recognition_reliability"]
                if reliability is None
                else min(reliability, context["recognition_reliability"])
            )
        expected = context["decision_expected_success"]
        conflict = context["outcome_conflict"]
        abstained = context["outcome_abstained"]
    return None, DiagnosisCase(
        ref=ref,
        scope=sample.scope,
        outcome=sample.outcome,
        outcome_confidence=sample.confidence,
        execution_status=sample.execution_status,
        label_status=label,
        recognition_reliability=reliability,
        decision_expected_success=expected,
        outcome_conflict=conflict,
        outcome_abstained=abstained,
    )


# {
#   責務: [_sample_label_status: 既存の操作効果sampleから格上げしない確定度を推定する]
#   処理: [実行済み・verified・決定論/人手の一致した出所だけverified_effect、校正済みはsupported_effect、
#     その他の既知効果はobserved_only、未知効果はunknownとする]
#   引数: [sample: 検証済みKnowledgeUpdateSample]
#   戻り値: [str: LABEL_STATUSESの一つ]
# }
def _sample_label_status(sample: KnowledgeUpdateSample) -> str:
    if sample.outcome == "unknown" or sample.execution_status != "executed":
        return "unknown"
    if sample.verified and sample.evidence_kind == sample.provenance_source:
        if sample.evidence_kind in _STRICT_EVIDENCE:
            return "verified_effect"
        if sample.evidence_kind in _SUPPORTED_EVIDENCE:
            return "supported_effect"
    return "observed_only"


# {
#   責務: [_contexts_by_outcome: 同じstepの診断文脈eventを検証し、操作効果event IDで引けるようにする]
#   処理: [fieldと値を検査し、不正なものと同じ効果eventへの重複を不正として数える]
#   引数: [events: stepのevent群]
#   戻り値: [tuple[dict[str, Mapping[str, Any]], int]: 効果event IDごとの文脈と不正件数]
# }
def _contexts_by_outcome(
    events: Iterable[ExperienceEvent],
) -> tuple[dict[str, Mapping[str, Any]], int]:
    valid: dict[str, Mapping[str, Any]] = {}
    duplicated: set[str] = set()
    invalid = 0
    for event in events:
        if event.event_type != CONTEXT_EVENT_TYPE:
            continue
        payload = dict(event.payload)
        if not _valid_context(payload):
            invalid += 1
            continue
        key = payload["outcome_event_id"]
        if key in valid or key in duplicated:
            # 同じ効果へ複数の文脈があれば、どれも採用せず全件を不正とする。
            invalid += 1 + (key in valid)
            valid.pop(key, None)
            duplicated.add(key)
            continue
        valid[key] = payload
    return valid, invalid


# {
#   責務: [_valid_context: 診断文脈payloadが宣言済みfieldと有界な値だけを持つか判定する]
#   処理: [field集合、確定度、bool、0..1の任意数値を検査する]
#   引数: [payload: 文脈eventのpayload]
#   戻り値: [bool: 契約に一致すればTrue]
# }
def _valid_context(payload: Mapping[str, Any]) -> bool:
    if set(payload) != CONTEXT_FIELDS:
        return False
    if (
        not isinstance(payload["outcome_event_id"], str)
        or not isinstance(payload["label_status"], str)
        or payload["label_status"] not in LABEL_STATUSES
    ):
        return False
    if (
        type(payload["outcome_conflict"]) is not bool
        or type(payload["outcome_abstained"]) is not bool
    ):
        return False
    for name in ("recognition_reliability", "decision_expected_success"):
        value = payload[name]
        if value is not None and (
            type(value) not in (int, float) or not 0 <= value <= 1
        ):
            return False
    return True


# {
#   責務: [_count: 理由ごとの件数辞書へ正の件数を加える]
#   処理: [0件は記録しない] 引数: [counts: 件数辞書, reason: 理由, amount: 加算数]
#   戻り値: [なし]
# }
def _count(counts: dict[str, int], reason: str, amount: int) -> None:
    if amount > 0:
        counts[reason] = counts.get(reason, 0) + amount
