"""Challenger更新提案のstrategy表・更新量・上限を版付きで固定する。"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite

CHALLENGER_UPDATE_RULE_VERSION = 1
STRATEGIES = ("calibration", "threshold", "prototype")
# 更新できるfailure_kindと、その更新先component。execution/outcome系は本版で更新しない。
FAILURE_COMPONENTS = (
    ("action_ineffective", "decision"),
    ("decision_overconfident", "decision"),
    ("decision_underestimated", "decision"),
    ("recognition_unreliable", "recognition"),
)
# 失敗・無効果の検証済みcaseで、負例prototypeとして追加してよいfailure_kind。
NEGATIVE_FAILURE_KINDS = ("action_ineffective", "decision_overconfident")
# 数値更新strategyの1回の更新量の絶対上限。intensityを最大にしても超えない。
HARD_MAX_STEP = 0.25
# 1提案が持つ根拠参照（prototypeとして追加する参照を含む）の上限。
MAX_PROPOSAL_REFS = 16
DEFAULT_STRATEGY_TABLE = (
    ("action_ineffective", ("prototype", "calibration")),
    ("decision_overconfident", ("calibration", "prototype")),
    ("decision_underestimated", ("calibration",)),
    ("recognition_unreliable", ("threshold", "calibration")),
)


# {
#   責務: [ChallengerUpdatePolicy: 更新提案のstrategy選択・更新量・件数上限を一つの指紋へ固定する]
#   フィールド: [strategy_table: (failure_kind, 優先順のstrategy群)の名前順, enabled_strategies: 使用を許すstrategy,
#     intensity: #426の正規化強度0..1。更新量の上限だけに作用する,
#     calibration_min_step/calibration_max_step/threshold_min_step/threshold_max_step: 数値更新量の範囲,
#     max_prototypes_per_proposal/max_prototypes: prototype追加と保持の上限,
#     max_proposals_per_target/max_proposals: 提案件数の上限,
#     minimum_strict_support: 提案に必要な確定根拠件数。2未満にできない]
# }
@dataclass(frozen=True, slots=True)
class ChallengerUpdatePolicy:
    strategy_table: tuple[tuple[str, tuple[str, ...]], ...] = DEFAULT_STRATEGY_TABLE
    enabled_strategies: tuple[str, ...] = STRATEGIES
    intensity: float = 0.5
    calibration_min_step: float = 0.02
    calibration_max_step: float = 0.2
    threshold_min_step: float = 0.02
    threshold_max_step: float = 0.15
    max_prototypes_per_proposal: int = 16
    max_prototypes: int = 256
    max_proposals_per_target: int = 2
    max_proposals: int = 64
    minimum_strict_support: int = 2

    # {
    #   責務: [__post_init__: strategy表・強度・更新量・上限を検査し、緩い設定や曖昧な表を拒否する]
    #   処理: [表のkeyを既知failure_kindの名前順・一意に、strategyを既知・一意に限る。
    #     数値は有限・範囲内、件数は有界整数、確定根拠の下限は2以上]
    #   引数: [self: 更新policy] 戻り値: [なし] エラー: [ValueError: 不正なpolicy]
    # }
    def __post_init__(self) -> None:
        table = self.strategy_table
        if type(table) is not tuple or any(
            type(row) is not tuple or len(row) != 2 for row in table
        ):
            raise ValueError("strategy table must be a tuple of pairs")
        kinds = [row[0] for row in table]
        updatable = dict(FAILURE_COMPONENTS)
        if kinds != sorted(set(kinds)) or any(kind not in updatable for kind in kinds):
            raise ValueError("strategy table keys must be unique sorted failure kinds")
        for kind, strategies in table:
            _strategy_tuple(strategies, allow_empty=True)
            if "prototype" in strategies and kind not in NEGATIVE_FAILURE_KINDS:
                raise ValueError("prototype is only for negative failure kinds")
        _strategy_tuple(self.enabled_strategies, allow_empty=False)
        _bounded_ratio(self.intensity, 0.0, 1.0)
        for low, high in (
            (self.calibration_min_step, self.calibration_max_step),
            (self.threshold_min_step, self.threshold_max_step),
        ):
            _bounded_ratio(low, 0.0, HARD_MAX_STEP)
            _bounded_ratio(high, 0.0, HARD_MAX_STEP)
            if not 0 < low <= high:
                raise ValueError("update steps must satisfy 0 < min <= max")
        limits = (
            (self.max_prototypes_per_proposal, 1, MAX_PROPOSAL_REFS),
            (self.max_prototypes, 1, 256),
            (self.max_proposals_per_target, 1, len(STRATEGIES)),
            (self.max_proposals, 1, 1024),
            (self.minimum_strict_support, 2, 65536),
        )
        if any(
            type(value) is not int or not low <= value <= high
            for value, low, high in limits
        ):
            raise ValueError("challenger update limits are out of range")

    # {
    #   責務: [strategies_for: failure_kindに対応する優先順のstrategyを返す]
    #   処理: [表に無いkindは空tupleとする] 引数: [failure_kind: #169のFAILURE_KINDSの一つ]
    #   戻り値: [tuple[str, ...]: 有効化の有無に関係なく表の並び]
    # }
    def strategies_for(self, failure_kind: str) -> tuple[str, ...]:
        return dict(self.strategy_table).get(failure_kind, ())

    # {
    #   責務: [max_step: 数値strategyの1回の更新量上限をintensityから求める]
    #   処理: [min + intensity*(max-min)をHARD_MAX_STEPで切る]
    #   引数: [strategy: calibrationまたはthreshold] 戻り値: [float: 0より大きい上限]
    #   エラー: [ValueError: 数値strategyではない]
    # }
    def max_step(self, strategy: str) -> float:
        if strategy == "calibration":
            low, high = self.calibration_min_step, self.calibration_max_step
        elif strategy == "threshold":
            low, high = self.threshold_min_step, self.threshold_max_step
        else:
            raise ValueError("strategy has no numeric step")
        return min(low + self.intensity * (high - low), HARD_MAX_STEP)

    # {
    #   責務: [fingerprint: 提案の再現に必要な規則版とpolicy値を短い指紋にする]
    #   処理: [規則版とfieldをsort済みJSONにしてSHA-256先頭16桁を返す]
    #   引数: [self: 更新policy] 戻り値: [str: 16桁の小文字hex]
    # }
    @property
    def fingerprint(self) -> str:
        content = {"rule_version": CHALLENGER_UPDATE_RULE_VERSION, **asdict(self)}
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode()).hexdigest()[:16]


# {
#   責務: [_strategy_tuple: strategy群が既知・一意のtupleであることを検査する]
#   処理: [型・既知値・重複を確認する]
#   引数: [value: 検査値, allow_empty: 空を許すか] 戻り値: [なし] エラー: [ValueError: 不正]
# }
def _strategy_tuple(value: object, *, allow_empty: bool) -> None:
    if (
        type(value) is not tuple
        or any(type(item) is not str for item in value)
        or (not value and not allow_empty)
        or len(set(value)) != len(value)
        or any(item not in STRATEGIES for item in value)
    ):
        raise ValueError("strategies must be a unique tuple of known strategies")


# {
#   責務: [_bounded_ratio: 値が有限で指定範囲内の数値であることを検査する]
#   処理: [bool・非数値・NaN・範囲外を拒否する]
#   引数: [value: 検査値, low/high: 閉区間] 戻り値: [なし] エラー: [ValueError: 不正な値]
# }
def _bounded_ratio(value: object, low: float, high: float) -> None:
    if (
        type(value) not in (int, float)
        or not isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError("policy value is out of range")
