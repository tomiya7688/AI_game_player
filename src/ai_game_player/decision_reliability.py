from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any

from ai_game_player.models import ActionDecision


# {
#   責務: [
#     ReliabilityStatus: 決定論的なDecision信頼性判定を4段階で表す
#   ]
#   フィールド: [
#     TRUST: 候補と現在の観測へ矛盾なく結び付いた
#     CAUTION: 判断理由が不足し、慎重な扱いが必要
#     VERIFY: 参照情報が不足し、追加確認が必要
#     REJECT: schema・候補・snapshot・文脈の検証に失敗した
#   ]
# }
class ReliabilityStatus(str, Enum):
    TRUST = "TRUST"
    CAUTION = "CAUTION"
    VERIFY = "VERIFY"
    REJECT = "REJECT"


@dataclass(frozen=True)
# {
#   責務: [
#     ReliabilityEvidence: 信頼性判定の検査結果とその出所を保持する
#   ]
#   フィールド: [
#     check: 実行した決定論的検査名
#     severity: 判定状態へ反映する深刻度
#     confidence: 検査結果への確信度
#     message: 人が読める検査結果
#     source: 検査規則の出所
#     details: 比較した参照値
#   ]
# }
class ReliabilityEvidence:
    check: str
    severity: str
    confidence: float
    message: str
    source: str = "decision_verifier/v1"
    details: dict[str, Any] = field(default_factory=dict)

    # {
    #   責務: [
    #     __post_init__: 信頼性Evidenceの必須値と確信度を検証する
    #   ]
    #   処理: [
    #     1: Evidenceの文字列項目を検証する
    #     2: 深刻度と確信度の範囲を検証する
    #   ]
    #   引数: []
    #   戻り値: []
    #   エラー: [
    #     ValueError: 必須値または確信度が不正
    #   ]
    # }
    def __post_init__(self) -> None:
        if not self.check.strip() or not self.message.strip() or not self.source.strip():
            raise ValueError("reliability evidence metadata must not be empty")
        if self.severity not in {"info", "caution", "verify", "reject"}:
            raise ValueError("invalid reliability evidence severity")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("reliability evidence confidence must be between 0 and 1")

    # {
    #   責務: [
    #     to_dict: 信頼性EvidenceをJSON保存可能な辞書へ変換する
    #   ]
    #   処理: [
    #     1: Evidenceの全項目を辞書へ格納する
    #   ]
    #   引数: []
    #   戻り値: [
    #     dict[str, Any]: 検査結果と出所
    #   ]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "confidence": self.confidence,
            "message": self.message,
            "source": self.source,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
# {
#   責務: [
#     ReliabilityResult: 検証状態・対象snapshot・監査Evidenceを保持する
#   ]
#   フィールド: [
#     assessment_id: 一意な判定ID
#     status: TRUST・CAUTION・VERIFY・REJECT
#     snapshot_id: 検証対象snapshot
#     action_id: 選択候補ID
#     provider: 判断Provider
#     evidence: 判定を再確認できる検査Evidence
#   ]
# }
class ReliabilityResult:
    assessment_id: str
    status: ReliabilityStatus
    snapshot_id: str
    action_id: str
    provider: str
    evidence: tuple[ReliabilityEvidence, ...]

    # {
    #   責務: [
    #     to_dict: 判定とEvidenceをschema付き監査辞書へ変換する
    #   ]
    #   処理: [
    #     1: 判定識別子と状態を格納する
    #     2: Evidenceを個別の記録へ変換する
    #   ]
    #   引数: []
    #   戻り値: [
    #     dict[str, Any]: reliability/v1の監査記録
    #   ]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "reliability/v1",
            "assessment_id": self.assessment_id,
            "status": self.status.value,
            "snapshot_id": self.snapshot_id,
            "action_id": self.action_id,
            "provider": self.provider,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
# {
#   責務: [
#     _DecisionFields: schema検査済みDecisionの値を内部で束ねる
#   ]
#   フィールド: [
#     action_id: 選択候補ID
#     reason: 選択理由
#     provider: 判断Provider
#     snapshot_id: 判断対象snapshot
#     screen_id: 判断対象scene
#     state_signature: 判断対象状態signature
#   ]
# }
class _DecisionFields:
    action_id: str
    reason: str
    provider: str
    snapshot_id: str | None
    screen_id: str | None
    state_signature: str | None


# {
#   責務: [
#     _decision_fields: 未信頼Decisionのschemaと型を検査して正規化する
#   ]
#   処理: [
#     1: ActionDecisionまたはMappingを読み取る
#     2: 不明な項目・必須項目・型・空文字を検証する
#     3: 検証済みフィールドを返す
#   ]
#   引数: [
#     decision_output: Providerから返された任意の値
#   ]
#   戻り値: [
#     _DecisionFields | None: 検証済みのDecision項目
#   ]
# }
def _decision_fields(decision_output: object) -> _DecisionFields | None:
    if isinstance(decision_output, ActionDecision):
        if decision_output.validation_error:
            return None
        record: Mapping[str, Any] = decision_output.to_dict()
    elif isinstance(decision_output, Mapping):
        record = decision_output
    else:
        return None

    allowed_fields = {"action_id", "reason", "provider", "snapshot_id", "screen_id", "state_signature"}
    if set(record).difference(allowed_fields):
        return None
    required_fields = ("action_id", "reason", "provider")
    if any(name not in record for name in required_fields):
        return None
    action_id, reason, provider = (record[name] for name in required_fields)
    if not all(isinstance(value, str) for value in (action_id, reason, provider)):
        return None
    if not action_id.strip() or not provider.strip():
        return None

    optional_fields: list[str | None] = []
    for field_name in ("snapshot_id", "screen_id", "state_signature"):
        field_value = record.get(field_name)
        if field_value is not None and not isinstance(field_value, str):
            return None
        optional_fields.append(field_value)
    return _DecisionFields(action_id, reason, provider, *optional_fields)
