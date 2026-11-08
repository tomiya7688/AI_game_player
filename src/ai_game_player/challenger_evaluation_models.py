"""#173 Candidate Evaluation Runnerの症例・方針・集計・評価Eventの型。

採否は行わない（#171のGate）。ここは値の検証と、保存用の辞書化だけを持つ。
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from typing import Any

from ai_game_player.training_diagnosis_models import DiagnosisCase

EVALUATION_EVENT_SCHEMA_VERSION = 1
GROUPS = ("target", "regression")
VALIDATION_STATES = ("offline", "real_world")
SIGNALS = ("de_escalate", "escalate", "hold")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
MAX_CASES = 1_000_000


# {
#   責務: [EvaluationError: 症例・方針・保存内容が契約に反することを機械可読な理由付きで伝える]
#   フィールド: [code: invalid_case/scope_mismatch/duplicate_case/invalid_policy/invalid_artifact/invalid_event]
# }
class EvaluationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# {
#   責務: [EvaluationCase: 同じ症例をChampionとChallengerの両方で採点するための、群つきの症例]
#   フィールド: [case: #169の診断症例, group: target(改善を見たい症例)かregression(悪化させたくない症例)]
# }
@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case: DiagnosisCase
    group: str

    def __post_init__(self) -> None:
        if type(self.case) is not DiagnosisCase or self.group not in GROUPS:
            raise EvaluationError("invalid_case", "case must be a DiagnosisCase in a known group")


# {
#   責務: [EvaluationPolicy: 評価の件数・改善幅・回帰許容・latency予算を保持する。採否基準(#171)とは独立]
#   フィールド: [min_scored_per_group: 群ごとの最小採点件数, min_gain: escalateに要る対象改善幅(0..1),
#     regression_tolerance: 回帰とみなさない低下幅(0..1), case_latency_budget_ms: 1症例の採点時間の上限]
# }
@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    min_scored_per_group: int = 10
    min_gain: float = 0.02
    regression_tolerance: float = 0.0
    case_latency_budget_ms: float = 50.0

    def __post_init__(self) -> None:
        if type(self.min_scored_per_group) is not int or not 1 <= self.min_scored_per_group <= MAX_CASES:
            raise EvaluationError("invalid_policy", "min_scored_per_group is out of range")
        for name in ("min_gain", "regression_tolerance"):
            if not _ratio(getattr(self, name)):
                raise EvaluationError("invalid_policy", f"{name} must be a ratio in [0, 1]")
        budget = self.case_latency_budget_ms
        if type(budget) not in (int, float) or not math.isfinite(budget) or not 0 < budget <= 60_000:
            raise EvaluationError("invalid_policy", "case_latency_budget_ms is out of range")

    @property
    def fingerprint(self) -> str:
        body = {name: getattr(self, name) for name in self.__slots__}
        return sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# {
#   責務: [ArmSummary: 一方(ChampionまたはChallenger)の、ある群での採点結果]
#   フィールド: [scored: 採点対象にした件数(失敗を含む), correct: 採否が確定ラベルと一致した件数,
#     failed: 例外または時間超過で誤り扱いにした件数, unscorable: 確定ラベルが無く採点しなかった件数,
#     accuracy: correct/scored(scored=0なら0.0), latency_p95_ms: 採点時間の95パーセンタイル]
# }
@dataclass(frozen=True, slots=True)
class ArmSummary:
    scored: int
    correct: int
    failed: int
    unscorable: int
    accuracy: float
    latency_p95_ms: float

    def __post_init__(self) -> None:
        counts = (self.scored, self.correct, self.failed, self.unscorable)
        if any(type(value) is not int or not 0 <= value <= MAX_CASES for value in counts):
            raise EvaluationError("invalid_event", "counts are out of range")
        if self.correct > self.scored or self.failed > self.scored or not _ratio(self.accuracy):
            raise EvaluationError("invalid_event", "summary is inconsistent")
        if type(self.latency_p95_ms) not in (int, float) or not math.isfinite(self.latency_p95_ms) or self.latency_p95_ms < 0:
            raise EvaluationError("invalid_event", "latency is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}


# {
#   責務: [EvaluationEvent: 1回の比較評価の結果・測定・調整信号・検証状態を、内容hash付きで保存できる形にする]
#   フィールド: [evaluation_ref/evaluated_at: 評価の識別子と測り終えた時刻, validation_state: offline(実世界検証の代替ではない)かreal_world,
#     artifact_id/champion_sha256/challenger_sha256: 比較した対象, policy_fingerprint: 使った方針,
#     groups: 群→{champion, challenger}のArmSummary, resource_cost: Challengerの資源見積り,
#     signal: 学習Levelへの助言(de_escalate/hold/escalate), reasons: 信号の固定理由コード]
# }
@dataclass(frozen=True, slots=True)
class EvaluationEvent:
    evaluation_ref: str
    evaluated_at: str
    validation_state: str
    artifact_id: str
    champion_sha256: str
    challenger_sha256: str
    policy_fingerprint: str
    groups: dict[str, dict[str, ArmSummary]]
    resource_cost: dict[str, Any]
    signal: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not IDENTIFIER.fullmatch(str(self.evaluation_ref))
            or not IDENTIFIER.fullmatch(str(self.artifact_id))
            or not TIMESTAMP.fullmatch(str(self.evaluated_at))
            or self.validation_state not in VALIDATION_STATES
            or self.signal not in SIGNALS
            or set(self.groups) != set(GROUPS)
            or any(set(arms) != {"champion", "challenger"} for arms in self.groups.values())
            or not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (self.champion_sha256, self.challenger_sha256))
        ):
            raise EvaluationError("invalid_event", "event fields do not match the contract")

    def to_dict(self) -> dict[str, Any]:
        content = {
            "schema": "learning-evaluation-event",
            "schema_version": EVALUATION_EVENT_SCHEMA_VERSION,
            "evaluation_ref": self.evaluation_ref,
            "evaluated_at": self.evaluated_at,
            "validation_state": self.validation_state,
            "artifact_id": self.artifact_id,
            "champion_sha256": self.champion_sha256,
            "challenger_sha256": self.challenger_sha256,
            "policy_fingerprint": self.policy_fingerprint,
            "groups": {
                group: {arm: summary.to_dict() for arm, summary in arms.items()}
                for group, arms in self.groups.items()
            },
            "resource_cost": self.resource_cost,
            "signal": self.signal,
            "reasons": list(self.reasons),
        }
        return {"event_id": "evaluation." + digest(content), **content}

    @classmethod
    def from_dict(cls, value: object) -> "EvaluationEvent":
        """保存辞書から復元し、event_idの再計算結果が一致しなければ拒否する。"""
        try:
            event = cls(
                evaluation_ref=value["evaluation_ref"],  # type: ignore[index]
                evaluated_at=value["evaluated_at"],  # type: ignore[index]
                validation_state=value["validation_state"],  # type: ignore[index]
                artifact_id=value["artifact_id"],  # type: ignore[index]
                champion_sha256=value["champion_sha256"],  # type: ignore[index]
                challenger_sha256=value["challenger_sha256"],  # type: ignore[index]
                policy_fingerprint=value["policy_fingerprint"],  # type: ignore[index]
                groups={
                    group: {arm: ArmSummary(**summary) for arm, summary in arms.items()}
                    for group, arms in value["groups"].items()  # type: ignore[index]
                },
                resource_cost=value["resource_cost"],  # type: ignore[index]
                signal=value["signal"],  # type: ignore[index]
                reasons=tuple(value["reasons"]),  # type: ignore[index]
            )
        except (KeyError, TypeError, AttributeError) as error:
            raise EvaluationError("invalid_event", str(error)) from error
        if event.to_dict() != value:
            raise EvaluationError("invalid_event", "event content does not match its digest")
        return event


def digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
    return sha256(body.encode()).hexdigest()


def _ratio(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)
