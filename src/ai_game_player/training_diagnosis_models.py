"""Training Value / Error Diagnosisの入力caseと版付き出力の型。

入力case、case単位の帰属、component単位の集計、報告全体は同じ診断契約の
一部として同時に変更されるため、小さな不変dataclassを一つのファイルにまとめる。
"""

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from typing import Any

from ai_game_player.knowledge_scope import KnowledgeScope
from ai_game_player.knowledge_update_sample import EXECUTION_STATUSES, OUTCOMES
from ai_game_player.learning_dataset import DatasetSource
from ai_game_player.training_diagnosis_policy import LABEL_STATUSES


TRAINING_DIAGNOSIS_SCHEMA_VERSION = 1
COMPONENTS = ("recognition", "decision", "execution", "outcome_interpretation")
FAILURE_KINDS = (
    "execution_failure",
    "execution_unconfirmed",
    "outcome_conflict",
    "outcome_unresolved",
    "recognition_unreliable",
    "decision_overconfident",
    "decision_underestimated",
    "action_ineffective",
)
# basis=receiptは実行receiptの事実、verified_labelは#647の確定ラベル、他は不確実な根拠。
BASES = ("receipt", "verified_label", "uncertain_label", "outcome_disagreement")
TARGET_STATUSES = ("candidate", "hypothesis")


# {
#   責務: [DiagnosisCase: 一つの実行操作について診断に使う型付き信号を保持する]
#   フィールド: [ref: 元Experienceのepisode/step/event参照, scope: Knowledge隔離scope,
#     outcome: success/no_effect/failure/unknown, outcome_confidence: 効果分類の確信度,
#     execution_status: 実行receiptの送信状態, label_status: #647の確定度。文脈eventで格上げしない,
#     recognition_reliability: 任意の認識校正値, decision_expected_success: 任意の判断時予測成功率,
#     outcome_conflict/outcome_abstained: Outcome検出器の矛盾・保留]
# }
@dataclass(frozen=True, slots=True)
class DiagnosisCase:
    ref: DatasetSource
    scope: KnowledgeScope
    outcome: str
    outcome_confidence: float
    execution_status: str
    label_status: str
    recognition_reliability: float | None = None
    decision_expected_success: float | None = None
    outcome_conflict: bool = False
    outcome_abstained: bool = False

    # {
    #   責務: [__post_init__: 列挙値と0..1の数値を検査し、不正な信号から診断しない]
    #   処理: [型・既知の列挙値・有限性・範囲を確認する] 引数: [self: case] 戻り値: [なし]
    #   エラー: [ValueError: 不正なcase]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.ref, DatasetSource) or not isinstance(
            self.scope, KnowledgeScope
        ):
            raise ValueError("diagnosis case requires a dataset reference and scope")
        if (
            self.outcome not in OUTCOMES
            or self.execution_status not in EXECUTION_STATUSES
            or self.label_status not in LABEL_STATUSES
            or type(self.outcome_conflict) is not bool
            or type(self.outcome_abstained) is not bool
        ):
            raise ValueError("diagnosis case classification is invalid")
        _ratio(self.outcome_confidence, "outcome_confidence")
        for name in ("recognition_reliability", "decision_expected_success"):
            if getattr(self, name) is not None:
                _ratio(getattr(self, name), name)


# {
#   責務: [ComponentAttribution: 一つのcaseの誤り・低価値を一つのcomponentへ帰属した候補]
#   フィールド: [component: COMPONENTSの一つ, failure_kind: FAILURE_KINDSの一つ,
#     confidence: 帰属の確信度0..1。正解の保証ではない, basis: BASESの一つ,
#     strict: receipt事実か#647確定ラベルに基づく場合だけTrue]
# }
@dataclass(frozen=True, slots=True)
class ComponentAttribution:
    component: str
    failure_kind: str
    confidence: float
    basis: str
    strict: bool

    # {
    #   責務: [__post_init__: 列挙値とstrictとbasisの整合を検査する]
    #   処理: [不確実なbasisでstrict=Trueを宣言する帰属を拒否する]
    #   引数: [self: 帰属] 戻り値: [なし] エラー: [ValueError: 不正な帰属]
    # }
    def __post_init__(self) -> None:
        if (
            self.component not in COMPONENTS
            or self.failure_kind not in FAILURE_KINDS
            or self.basis not in BASES
            or type(self.strict) is not bool
        ):
            raise ValueError("component attribution is invalid")
        if self.strict and self.basis not in ("receipt", "verified_label"):
            raise ValueError("an uncertain basis cannot support a strict attribution")
        _ratio(self.confidence, "confidence")

    # {
    #   責務: [to_dict: 帰属をJSON互換の辞書にする]
    #   処理: [fieldをそのまま並べる] 引数: [self: 帰属] 戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "failure_kind": self.failure_kind,
            "confidence": self.confidence,
            "basis": self.basis,
            "strict": self.strict,
        }


# {
#   責務: [CaseDiagnosis: 一つのcaseのtraining valueと原因候補を保持する]
#   フィールド: [ref: 元Experience参照, scope: Knowledge scope, label_status: 採用した確定度,
#     training_value: 学習対象としての価値0..1。保持(storage)価値とは別軸,
#     attributions: component帰属候補。確信度降順]
# }
@dataclass(frozen=True, slots=True)
class CaseDiagnosis:
    ref: DatasetSource
    scope: KnowledgeScope
    label_status: str
    training_value: float
    attributions: tuple[ComponentAttribution, ...]

    # {
    #   責務: [to_dict: case診断をJSON互換の辞書にする]
    #   処理: [参照・scope・帰属を辞書化する] 引数: [self: case診断]
    #   戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref.to_dict(),
            "scope": self.scope.to_dict(),
            "label_status": self.label_status,
            "training_value": self.training_value,
            "attributions": [item.to_dict() for item in self.attributions],
        }


# {
#   責務: [ComponentTarget: scopeごとの学習対象component候補と根拠を集計する]
#   フィールド: [component: COMPONENTSの一つ, scope: Knowledge scope,
#     status: candidateは確定根拠が閾値以上、hypothesisは不確実な根拠だけ,
#     confidence: 帰属確信度の結合値。hypothesisは上限付き, strict_support/uncertain_support: 根拠件数,
#     failure_kinds: (FAILURE_KIND, 件数)の名前順, evidence_refs: 確信度上位の元Experience参照]
# }
@dataclass(frozen=True, slots=True)
class ComponentTarget:
    component: str
    scope: KnowledgeScope
    status: str
    confidence: float
    strict_support: int
    uncertain_support: int
    failure_kinds: tuple[tuple[str, int], ...]
    evidence_refs: tuple[DatasetSource, ...]

    # {
    #   責務: [__post_init__: statusと確定根拠件数の整合を検査する]
    #   処理: [確定根拠0件のcandidateを拒否する] 引数: [self: target] 戻り値: [なし]
    #   エラー: [ValueError: 不正なtarget]
    # }
    def __post_init__(self) -> None:
        if self.component not in COMPONENTS or self.status not in TARGET_STATUSES:
            raise ValueError("component target is invalid")
        if self.status == "candidate" and self.strict_support < 1:
            raise ValueError("a candidate target requires strict support")
        _ratio(self.confidence, "confidence")

    # {
    #   責務: [to_dict: targetをJSON互換の辞書にする]
    #   処理: [scope・件数・参照を辞書化する] 引数: [self: target]
    #   戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "scope": self.scope.to_dict(),
            "status": self.status,
            "confidence": self.confidence,
            "strict_support": self.strict_support,
            "uncertain_support": self.uncertain_support,
            "failure_kinds": [list(item) for item in self.failure_kinds],
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
        }


# {
#   責務: [TrainingDiagnosisReport: 診断全体の版付き・有界な出力を保持する]
#   フィールド: [policy_fingerprint: 使用policyの指紋, cases_considered: 診断したcase数,
#     excluded: (除外理由, 件数)の名前順, findings: training value上位のcase診断,
#     findings_truncated/targets_truncated: 上限で切り詰めたか, targets: component候補,
#     schema_version: 出力契約の版]
# }
@dataclass(frozen=True, slots=True)
class TrainingDiagnosisReport:
    policy_fingerprint: str
    cases_considered: int
    excluded: tuple[tuple[str, int], ...]
    findings: tuple[CaseDiagnosis, ...]
    findings_truncated: bool
    targets: tuple[ComponentTarget, ...]
    targets_truncated: bool
    schema_version: int = TRAINING_DIAGNOSIS_SCHEMA_VERSION

    # {
    #   責務: [to_dict: 報告をdiagnosis_id付きのJSON互換辞書にする]
    #   処理: [内容を辞書化し、内容のSHA-256をdiagnosis_idとして加える]
    #   引数: [self: 報告] 戻り値: [dict[str, Any]: JSON互換値]
    # }
    def to_dict(self) -> dict[str, Any]:
        content = {
            "schema": "training-diagnosis",
            "schema_version": self.schema_version,
            "policy_fingerprint": self.policy_fingerprint,
            "cases_considered": self.cases_considered,
            "excluded": [list(item) for item in self.excluded],
            "findings": [item.to_dict() for item in self.findings],
            "findings_truncated": self.findings_truncated,
            "targets": [item.to_dict() for item in self.targets],
            "targets_truncated": self.targets_truncated,
        }
        return {"diagnosis_id": "diagnosis." + _digest(content), **content}

    # {
    #   責務: [diagnosis_id: 同じ入力とpolicyから同じ値になる報告識別子を返す]
    #   処理: [to_dictのdiagnosis_idを返す] 引数: [self: 報告]
    #   戻り値: [str: diagnosis.<SHA-256>]
    # }
    @property
    def diagnosis_id(self) -> str:
        return self.to_dict()["diagnosis_id"]


# {
#   責務: [_ratio: 確信度などの値が有限な0..1の数値であることを検査する]
#   処理: [bool・非数値・NaN・範囲外を拒否する]
#   引数: [value: 検査値, name: エラーに使う固定field名] 戻り値: [なし]
#   エラー: [ValueError: 不正な値]
# }
def _ratio(value: object, name: str) -> None:
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a finite number within 0..1")


# {
#   責務: [_digest: JSON互換値の正規化表現からSHA-256を作る]
#   処理: [key順・区切りを固定し、NaNを拒否してhashする]
#   引数: [value: JSON互換値] 戻り値: [str: 64桁の小文字hex]
# }
def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return sha256(encoded).hexdigest()
