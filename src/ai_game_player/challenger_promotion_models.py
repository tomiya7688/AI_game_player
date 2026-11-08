"""#171 Promotion / Rollback Gateの基準・評価入力・判定の型。

採否の計算は`challenger_promotion.ChampionRegistry`が行う。ここは値の検証だけを持つ。
"""

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from typing import Any

PROMOTION_SCHEMA_VERSION = 1
MAX_CASES = 1_000_000
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
ROLLBACK_REASONS = ("operator_request", "regression_detected", "safety_stop")


# {
#   責務: [PromotionError: Gateの入力・状態・保存内容が契約に反することを機械可読な理由付きで伝える]
#   フィールド: [code: not_initialized/already_initialized/invalid_input/invalid_state/no_rollback_target]
# }
class PromotionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# {
#   責務: [PromotionCriteria: 昇格に必要な最低条件を保持する。条件は強める方向にだけ変更する前提]
#   フィールド: [min_target_gain: 対象指標の最小改善幅(0より大きく1以下), max_regression_drop: 回帰指標の許容低下幅(0以上1以下、既定0),
#     min_target_cases: 対象評価の最小件数, min_regression_cases: 回帰評価の最小件数,
#     max_parameter_bytes: Challenger parameterの上限byte, max_delta_bytes: Championからの増加上限byte]
# }
@dataclass(frozen=True, slots=True)
class PromotionCriteria:
    min_target_gain: float = 0.02
    max_regression_drop: float = 0.0
    min_target_cases: int = 10
    min_regression_cases: int = 10
    max_parameter_bytes: int = 65536
    max_delta_bytes: int = 16384

    # {
    #   責務: [__post_init__: 各基準の型と範囲を検査する]
    #   処理: [比率は有限な範囲内、件数とbyteは正の上限内であることを要求する] 引数: [self: 基準] 戻り値: [なし]
    #   エラー: [PromotionError: invalid_input]
    # }
    def __post_init__(self) -> None:
        if not (_ratio(self.min_target_gain) and 0 < self.min_target_gain):
            raise PromotionError("invalid_input", "min_target_gain must be in (0, 1]")
        if not _ratio(self.max_regression_drop):
            raise PromotionError("invalid_input", "max_regression_drop must be in [0, 1]")
        for name in ("min_target_cases", "min_regression_cases"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= MAX_CASES:
                raise PromotionError("invalid_input", f"{name} is out of range")
        for name in ("max_parameter_bytes", "max_delta_bytes"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 16 * 1024 * 1024:
                raise PromotionError("invalid_input", f"{name} is out of range")

    # {
    #   責務: [fingerprint: 基準全体の短い識別子を返し、監査でどの基準で判定したかを追えるようにする]
    #   処理: [正規化JSONのSHA-256先頭16桁] 引数: [self: 基準] 戻り値: [str]
    # }
    @property
    def fingerprint(self) -> str:
        body = json.dumps(
            {name: getattr(self, name) for name in self.__slots__},
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(body.encode()).hexdigest()[:16]


# {
#   責務: [CandidateEvaluation: #173系の比較評価がChampionとChallengerを同じケースで測った結果を保持する。passedフラグは持たない]
#   フィールド: [evaluation_ref: 評価実行の識別子, artifact_id: 評価対象のChallenger artifact, champion_sha256/challenger_sha256: 比較したparameterの内容hash,
#     target_cases/regression_cases: 失敗・Timeoutを含めた母数, target_*: 対象指標(0..1、大きいほど良い), regression_*: 回帰指標(0..1、大きいほど良い)]
# }
@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    evaluation_ref: str
    artifact_id: str
    champion_sha256: str
    challenger_sha256: str
    target_cases: int
    regression_cases: int
    target_baseline: float
    target_candidate: float
    regression_baseline: float
    regression_candidate: float

    # {
    #   責務: [__post_init__: 識別子・hash・件数・指標の型と範囲を検査する]
    #   処理: [指標は有限な0..1、件数は正の上限内、hashは64桁hexを要求する] 引数: [self: 評価] 戻り値: [なし]
    #   エラー: [PromotionError: invalid_input]
    # }
    def __post_init__(self) -> None:
        for name in ("evaluation_ref", "artifact_id"):
            if type(getattr(self, name)) is not str or not IDENTIFIER_PATTERN.fullmatch(getattr(self, name)):
                raise PromotionError("invalid_input", f"{name} is not a bounded identifier")
        for name in ("champion_sha256", "challenger_sha256"):
            if type(getattr(self, name)) is not str or not SHA256_PATTERN.fullmatch(getattr(self, name)):
                raise PromotionError("invalid_input", f"{name} must be a sha256 hex digest")
        for name in ("target_cases", "regression_cases"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= MAX_CASES:
                raise PromotionError("invalid_input", f"{name} is out of range")
        for name in ("target_baseline", "target_candidate", "regression_baseline", "regression_candidate"):
            if not _ratio(getattr(self, name)):
                raise PromotionError("invalid_input", f"{name} must be a finite ratio in [0, 1]")


# {
#   責務: [PromotionDecision: 1回の採否の結論と根拠コードを返す。pointerを変えたかは`promoted`で分かる]
#   フィールド: [status: promoted/rejected, reasons: 固定コードの一覧(promotedなら空), generation: 確定後のregistry世代,
#     champion_version: 判定後に本番となる版, criteria_fingerprint: 判定に使った基準]
# }
@dataclass(frozen=True, slots=True)
class PromotionDecision:
    status: str
    reasons: tuple[str, ...]
    generation: int
    champion_version: str
    criteria_fingerprint: str

    @property
    def promoted(self) -> bool:
        return self.status == "promoted"


def _ratio(value: object) -> bool:
    # 範囲を先に確認する（10**1000のような巨大な整数でmath.isfiniteがOverflowErrorにならないように）。
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def to_json_bytes(value: Any) -> bytes:
    """状態保存用の正規化JSON。NaNは拒否する。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()
