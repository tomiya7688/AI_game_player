"""Training Value / Error Diagnosisの重み・上限・閾値を版付きで固定する。"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite


TRAINING_DIAGNOSIS_RULE_VERSION = 1
# #647のcausal_statusを小文字化した確定度の段階。verified_effectだけが確定ラベル。
LABEL_STATUSES = (
    "verified_effect",
    "supported_effect",
    "expected_consistent",
    "observed_only",
    "conflict",
    "unknown",
)
_LABEL_RANK = {name: rank for rank, name in enumerate(reversed(LABEL_STATUSES))}


# {
#   責務: [weaker_label_status: 二つの確定度のうち弱い方を返し、文脈eventによる格上げを防ぐ]
#   処理: [LABEL_STATUSESの並び順で比較し、確定度が低い段階を採用する]
#   引数: [first/second: LABEL_STATUSESに含まれる確定度]
#   戻り値: [str: 弱い方の確定度]
#   エラー: [ValueError: 未知の確定度]
# }
def weaker_label_status(first: str, second: str) -> str:
    if first not in _LABEL_RANK or second not in _LABEL_RANK:
        raise ValueError("label status is not a diagnosis label status")
    return first if _LABEL_RANK[first] <= _LABEL_RANK[second] else second


# {
#   責務: [TrainingDiagnosisPolicy: 診断の重み・上限・閾値を一つの指紋へ固定する]
#   フィールド: [verified/supported/expected_consistent/observed_only/conflict/unknown_label_weight:
#     確定度ごとの帰属確信度の倍率, uncertain_confidence_cap: 確定ラベル以外の帰属確信度上限,
#     low_reliability: 認識信頼度をrecognition原因候補とみなす上限,
#     overconfident_expectation: 失敗時にdecisionの過信とみなす予測成功率下限,
#     underestimated_expectation: 成功時にdecisionの過小評価とみなす予測成功率上限,
#     minimum_strict_support: 更新提案の候補(candidate)とする確定根拠の最小件数,
#     max_cases/max_findings/max_targets/max_evidence_refs: 入力と出力の件数上限]
# }
@dataclass(frozen=True, slots=True)
class TrainingDiagnosisPolicy:
    verified_label_weight: float = 1.0
    supported_label_weight: float = 0.6
    expected_consistent_label_weight: float = 0.35
    observed_only_label_weight: float = 0.25
    conflict_label_weight: float = 0.5
    unknown_label_weight: float = 0.15
    uncertain_confidence_cap: float = 0.5
    low_reliability: float = 0.5
    overconfident_expectation: float = 0.7
    underestimated_expectation: float = 0.3
    minimum_strict_support: int = 2
    max_cases: int = 4096
    max_findings: int = 256
    max_targets: int = 64
    max_evidence_refs: int = 16

    # {
    #   責務: [__post_init__: 重みと閾値が有限な0..1、件数上限が正の有界整数であることを検査する]
    #   処理: [bool・非数値・NaN・範囲外を拒否し、確定ラベルの重みが他より小さい設定を拒否する]
    #   引数: [self: 診断policy] 戻り値: [なし]
    #   エラー: [ValueError: 不正なpolicy]
    # }
    def __post_init__(self) -> None:
        ratios = (
            self.verified_label_weight,
            self.supported_label_weight,
            self.expected_consistent_label_weight,
            self.observed_only_label_weight,
            self.conflict_label_weight,
            self.unknown_label_weight,
            self.uncertain_confidence_cap,
            self.low_reliability,
            self.overconfident_expectation,
            self.underestimated_expectation,
        )
        if any(
            type(value) not in (int, float)
            or not isfinite(value)
            or not 0 <= value <= 1
            for value in ratios
        ):
            raise ValueError("diagnosis weights and thresholds must be within 0..1")
        if max(ratios[1:6]) > self.verified_label_weight:
            raise ValueError("an uncertain label must not outweigh a verified label")
        limits = (
            self.minimum_strict_support,
            self.max_cases,
            self.max_findings,
            self.max_targets,
            self.max_evidence_refs,
        )
        if any(type(value) is not int or not 1 <= value <= 65536 for value in limits):
            raise ValueError("diagnosis limits must be bounded positive integers")

    # {
    #   責務: [label_weight: 確定度に対応する帰属確信度の倍率を返す]
    #   処理: [LABEL_STATUSESの名前をpolicy fieldへ対応付ける]
    #   引数: [label_status: LABEL_STATUSESの一つ]
    #   戻り値: [float: 0..1の倍率]
    #   エラー: [ValueError: 未知の確定度]
    # }
    def label_weight(self, label_status: str) -> float:
        if label_status not in LABEL_STATUSES:
            raise ValueError("label status is not a diagnosis label status")
        return float(
            getattr(self, label_status.replace("_effect", "") + "_label_weight")
        )

    # {
    #   責務: [fingerprint: 診断結果の再現に必要な規則版とpolicy値を短い指紋にする]
    #   処理: [規則版とfieldをsort済みJSONにしてSHA-256先頭16桁を返す]
    #   引数: [self: 診断policy]
    #   戻り値: [str: 16桁の小文字hex]
    # }
    @property
    def fingerprint(self) -> str:
        content = {"rule_version": TRAINING_DIAGNOSIS_RULE_VERSION, **asdict(self)}
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode()).hexdigest()[:16]
