from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any

from ai_game_player.models import ActionDecision


# {
#   責務: [
#     ReliabilityStatus: 検査結果にもとづく候補選択の扱い方を4段階で表す
#   ]
#   フィールド: [
#     TRUST: 実施した検査を通過した（誤りが絶対にない保証ではない）
#     CAUTION: 判断理由が空など情報不足のため、慎重な扱いが必要
#     VERIFY: 画面や状態の参照情報が不足し、追加確認が必要
#     REJECT: 出力形式・許可候補・画面状態のいずれかが検査に失敗した
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
#     ReliabilityResult: 候補選択の検査結果と、後から確認するための記録を保持する
#   ]
#   フィールド: [
#     assessment_id: この検査結果を識別するID
#     status: 検査結果の扱い方（TRUST/CAUTION/VERIFY/REJECT）
#     snapshot_id: 判断時の画面観測を識別するID
#     action_id: Providerが選んだ許可候補のID
#     provider: 判断を返したProviderまたはモデル名
#     evidence: 実施した検査・結果・比較した値の記録
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
#     _DecisionFields: 出力形式を確認したDecisionの値を扱いやすく束ねる
#   ]
#   フィールド: [
#     action_id: Providerが選んだ候補のID
#     reason: 選択理由
#     provider: 判断を返したProviderまたはモデル名
#     snapshot_id: 判断時の画面観測を識別するID
#     screen_id: 判断時に観測した画面のID
#     state_signature: 判断時の画面状態を照合する文字列
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
