from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from ai_game_player.outcome import OutcomeAssessment


@dataclass(frozen=True)
class OutcomeEvidence:
    detector: str
    signal: str
    value: str
    confidence: float
    reliability: float
    provenance: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.detector.strip() or not self.signal.strip() or not self.value.strip():
            raise ValueError("outcome evidence metadata must not be empty")
        if not self.provenance.strip():
            raise ValueError("outcome evidence provenance must not be empty")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("outcome evidence confidence must be between 0 and 1")
        if not isfinite(self.reliability) or not 0.0 <= self.reliability <= 1.0:
            raise ValueError("outcome evidence reliability must be between 0 and 1")

    @property
    def weight(self) -> float:
        return self.confidence * self.reliability

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "signal": self.signal,
            "value": self.value,
            "confidence": self.confidence,
            "reliability": self.reliability,
            "provenance": self.provenance,
            "details": self.details.copy(),
        }


@dataclass(frozen=True)
# {
#   責務: [
#     OutcomeEvent: 操作後に推定したゲーム結果と、画面状態が変化したかを別々に記録する
#   ]
#   フィールド: [
#     action_id: 結果を結び付ける、直前に実行した操作候補のID
#     status: success/failureはゲームの終端結果、changed/unchangedは状態遷移、unknownは根拠不足を表す
#     confidence: 総合結果の根拠の強さを0から1で示す。値が高くても正解を保証するものではない
#     conflict: 成功と失敗、または状態変化と安定を複数の根拠が同時に支持した場合True
#     abstained: 根拠が割れるか不足し、success/failureを断定せず保留した場合True
#     evidence: 各検出器の結論、重み、確信度、判定元を残した一覧
#     reason: 総合判定を選んだ主な理由
#     semantic_fallback_used: 規則ベースの観測が弱い際に意味評価器も利用した場合True
#     state_changed: 状態差分の検出器が変化を支持したか。success/failureの終端結果とは別に保存する
#   ]
# }
class OutcomeEvent:
    action_id: str
    status: str
    confidence: float
    conflict: bool
    abstained: bool
    evidence: tuple[OutcomeEvidence, ...]
    reason: str
    semantic_fallback_used: bool = False
    state_changed: bool = False

    VALID_STATUSES = {"success", "failure", "changed", "unchanged", "unknown"}

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise ValueError("outcome event action_id must not be empty")
        if self.status not in self.VALID_STATUSES:
            raise ValueError(f"invalid outcome event status: {self.status}")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("outcome event confidence must be between 0 and 1")

    def to_assessment(self) -> OutcomeAssessment:
        status = self.status if self.status in {"success", "failure", "unknown"} else "ongoing"
        return OutcomeAssessment(status, self.confidence, self.reason)

    def to_evaluation_signals(self) -> dict[str, float]:
        signals: dict[str, float] = {}
        if self.status == "changed":
            signals["novelty"] = self.confidence
            signals["novelty_confidence"] = self.confidence
        elif self.status == "unchanged":
            signals["repetition"] = self.confidence
            signals["repetition_confidence"] = self.confidence

        for item in self.evidence:
            if item.signal != "state_delta":
                continue
            progress_delta = item.details.get("progress_delta")
            if isinstance(progress_delta, (int, float)) and isfinite(float(progress_delta)):
                signals["progress_delta"] = max(-1.0, min(1.0, float(progress_delta)))
                signals["progress_confidence"] = item.confidence
            resource_delta = item.details.get("resource_delta")
            if isinstance(resource_delta, (int, float)) and isfinite(float(resource_delta)):
                signals["resource_delta"] = max(-1.0, min(1.0, float(resource_delta)))
                signals["resources_confidence"] = item.confidence
        return signals

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "outcome-event/v1",
            "action_id": self.action_id,
            "status": self.status,
            "confidence": self.confidence,
            "conflict": self.conflict,
            "abstained": self.abstained,
            "semantic_fallback_used": self.semantic_fallback_used,
            "state_changed": self.state_changed,
            "reason": self.reason,
            "evidence": [item.to_dict() for item in self.evidence],
        }
