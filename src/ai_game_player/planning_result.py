"""観測に束縛した仮想探索の数値結果。実入力の許可を含まない。"""

from dataclasses import dataclass
import math
from typing import cast

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.belief_validation import belief_id, belief_integer, belief_number
from ai_game_player.planning_outcome import PlanningOutcome


# {
#   責務: [_quality: qualityを有限[-1,1]へ限定しboolを拒否する]
#   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
#   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
#   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
# }
def _quality(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not -1 <= value <= 1
        or not math.isfinite(value)
    ):
        raise ValueError("planning quality must be finite in [-1,1]")
    return float(value)


# {
#   責務: [WeightedValue: 排他的な仮想leafの絶対または明示条件付きchance質量を保持する]
#   フィールド: [value: normalized qualityの点評価, probability: 当該分布での質量,
#     lower_bound/upper_bound: quality支持不足の保守的区間。統計CIやworld確率ではない]
# }
@dataclass(frozen=True, slots=True)
class WeightedValue:
    value: float
    probability: float
    lower_bound: float | None = None
    upper_bound: float | None = None

    # {
    #   責務: [__post_init__: 有限qualityと区間、world/chance質量を別々に検証する]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        _quality(self.value)
        belief_number(self.probability, 1.0)
        lower = self.value if self.lower_bound is None else _quality(self.lower_bound)
        upper = self.value if self.upper_bound is None else _quality(self.upper_bound)
        if not lower <= self.value <= upper:
            raise ValueError("planning value interval is inconsistent")
        object.__setattr__(self, "lower_bound", lower)
        object.__setattr__(self, "upper_bound", upper)


# {
#   責務: [HypothesisActionValue: 一つのworldに条件付けたaction評価を保持する]
#   フィールド: [hypothesis_id: 元候補, belief_weight: 校正済みworld質量。不明ならNone,
#     relative_score: 未校正候補の比較値。確率にはしない,
#     expected_quality/lower_bound/upper_bound: 当該worldでのchance評価と区間,
#     conditional_outcomes: 当該world条件付きのleaf質量, trace_unknown_mass: 未保持leaf質量]
# }
@dataclass(frozen=True, slots=True)
class HypothesisActionValue:
    hypothesis_id: str
    belief_weight: float | None
    relative_score: float | None
    expected_quality: float
    lower_bound: float
    upper_bound: float
    conditional_outcomes: tuple[WeightedValue, ...] = ()
    trace_unknown_mass: float = 1.0

    # {
    #   責務: [__post_init__: 確率とscoreを区別し条件付き分布の質量を保存する]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        belief_id(self.hypothesis_id)
        if self.belief_weight is not None:
            belief_number(self.belief_weight, 1.0)
        if self.relative_score is not None:
            belief_number(self.relative_score)
        if (self.belief_weight is None) == (self.relative_score is None):
            raise ValueError(
                "planning hypothesis requires probability or relative score"
            )
        if (
            not _quality(self.lower_bound)
            <= _quality(self.expected_quality)
            <= _quality(self.upper_bound)
        ):
            raise ValueError("planning hypothesis value interval is inconsistent")
        _trace(self.conditional_outcomes, self.trace_unknown_mass)


# {
#   責務: [PlanningActionValue: 同じ合法actionを複数worldとchanceにわたり比較する]
#   フィールド: [action_id: verified候補と共通合法集合のID, expected_quality: 校正済みglobal点評価。不明ならNone,
#     lower_bound/upper_bound: 未表現worldも含むglobal区間, outcomes: belief×chanceの絶対質量,
#     trace_unknown_mass: 切詰め/未表現質量, hypothesis_values: world条件付き評価,
#     ranking_score: 比較用評価。未校正の場合もglobal期待値とは呼ばない]
# }
@dataclass(frozen=True, slots=True)
class PlanningActionValue:
    action_id: str
    expected_quality: float | None
    lower_bound: float
    upper_bound: float
    outcomes: tuple[WeightedValue, ...]
    trace_unknown_mass: float
    hypothesis_values: tuple[HypothesisActionValue, ...]
    ranking_score: float

    # {
    #   責務: [__post_init__: Actionの値域と有界な仮説集合・leaf分布を検証する]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        belief_id(self.action_id)
        lower, upper = _quality(self.lower_bound), _quality(self.upper_bound)
        if lower > upper:
            raise ValueError("planning action interval is inconsistent")
        if (
            self.expected_quality is not None
            and not lower - 1e-12 <= _quality(self.expected_quality) <= upper + 1e-12
        ):
            raise ValueError("planning expected quality exceeds interval")
        _quality(self.ranking_score)
        _trace(self.outcomes, self.trace_unknown_mass)
        if (
            type(self.hypothesis_values) is not tuple
            or len(self.hypothesis_values) > 256
            or any(
                not isinstance(item, HypothesisActionValue)
                for item in self.hypothesis_values
            )
        ):
            raise ValueError("planning hypothesis values exceed contract")
        if len({item.hypothesis_id for item in self.hypothesis_values}) != len(
            self.hypothesis_values
        ):
            raise ValueError("planning hypothesis values repeat identity")


# {
#   責務: [PlanningCounters: 探索の有限な実行コストを結果へ付ける]
#   フィールド: [nodes_visited: cache照合も含むnode訪問, chance_branches: 検査分岐,
#     leaf_evaluations: evaluator呼出数, cache_hits/peak_cache_entries/cache_bytes: cache診断,
#     worker_elapsed_ns: 子の独立clockで測る所要時間。親の期限とは比較しない]
# }
@dataclass(frozen=True, slots=True)
class PlanningCounters:
    nodes_visited: int = 0
    chance_branches: int = 0
    leaf_evaluations: int = 0
    cache_hits: int = 0
    peak_cache_entries: int = 0
    cache_bytes: int = 0
    worker_elapsed_ns: int = 0

    # {
    #   責務: [__post_init__: 非負整数の診断だけを保持しboolを拒否する]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        for value in (
            self.nodes_visited,
            self.chance_branches,
            self.leaf_evaluations,
            self.cache_hits,
            self.peak_cache_entries,
            self.cache_bytes,
            self.worker_elapsed_ns,
        ):
            belief_integer(value)


# {
#   責務: [PlanningResult: 完全に評価した深さの提案を元観測と実行世代へ束縛する]
#   フィールド: [outcome/selected_action_id: 既存Baseline文字列互換の提案。実入力はしない,
#     action_values: 共通合法actionだけの評価, context/belief_version: 入力観測/仮説版,
#     allowed_action_ids: 呼出側verified候補, probability_semantics: calibrated/conditional_score/unknown,
#     residual_unknown_mass: 元Beliefの未知質量, completed_depth: 全root actionが完了した深さ,
#     reason: 固定終了理由, model_id/model_version/evaluator_id/evaluator_version: 登録元,
#     quality_projection_version/quality_profile_version/value_unit: 数値の意味と版,
#     created_at_ns/expires_at_ns/compute_cutoff_at_ns: 親clockの受領/観測失効/計算受領期限,
#     counters: 子の有限コスト, limit_enforcement: 実際のOS制限方式, run_generation: 共有Run世代,
#     continuation_semantics: 共通操作列と完全将来観測のpolicy仮定を区別]
# }
@dataclass(frozen=True, slots=True)
class PlanningResult:
    outcome: PlanningOutcome
    selected_action_id: str | None
    action_values: tuple[PlanningActionValue, ...]
    context: BeliefContext
    belief_version: int
    allowed_action_ids: tuple[str, ...]
    probability_semantics: str
    residual_unknown_mass: float
    completed_depth: int
    reason: str
    model_id: str
    model_version: str
    evaluator_id: str
    evaluator_version: str
    quality_projection_version: int
    quality_profile_version: str
    value_unit: str
    created_at_ns: int
    expires_at_ns: int
    compute_cutoff_at_ns: int
    counters: PlanningCounters
    limit_enforcement: str
    run_generation: int
    continuation_semantics: str = "one_step"

    # {
    #   責務: [__post_init__: 同じ仮説集合の質量・global値・許可Action・版・期限を整合させる]
    #   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
    #   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
    #   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
    # }
    def __post_init__(self) -> None:
        if (
            not isinstance(self.outcome, PlanningOutcome)
            or not isinstance(self.context, BeliefContext)
            or not isinstance(self.counters, PlanningCounters)
        ):
            raise ValueError("planning result requires typed contracts")
        belief_integer(self.belief_version, 1)
        belief_integer(self.completed_depth, 0, 32)
        if (
            self.continuation_semantics
            not in {"one_step", "open_loop_mpc", "state_observed_expectimax"}
            or (self.completed_depth > 1 and self.continuation_semantics == "one_step")
            or (self.completed_depth <= 1 and self.continuation_semantics != "one_step")
        ):
            raise ValueError("planning continuation policy semantics mismatch")
        belief_integer(self.quality_projection_version, 1)
        belief_integer(self.created_at_ns)
        belief_integer(self.expires_at_ns)
        belief_integer(self.compute_cutoff_at_ns)
        belief_integer(self.run_generation)
        belief_number(self.residual_unknown_mass, 1.0)
        for value in (
            self.reason,
            self.model_id,
            self.model_version,
            self.evaluator_id,
            self.evaluator_version,
            self.quality_profile_version,
            self.value_unit,
            self.limit_enforcement,
        ):
            belief_id(value)
        if self.probability_semantics not in (
            "calibrated",
            "conditional_score",
            "unknown",
        ):
            raise ValueError("planning probability semantics are invalid")
        if (
            type(self.allowed_action_ids) is not tuple
            or len(self.allowed_action_ids) > 32
            or len(set(self.allowed_action_ids)) != len(self.allowed_action_ids)
        ):
            raise ValueError("planning allowed action set exceeds contract")
        for allowed_id in self.allowed_action_ids:
            belief_id(allowed_id)
        if (
            type(self.action_values) is not tuple
            or len(self.action_values) > 32
            or any(
                not isinstance(item, PlanningActionValue)
                or item.action_id not in self.allowed_action_ids
                for item in self.action_values
            )
        ):
            raise ValueError("planning action values exceed allowed candidates")
        if len({item.action_id for item in self.action_values}) != len(
            self.action_values
        ):
            raise ValueError("planning action values repeat identity")
        if self.outcome == PlanningOutcome.SELECT:
            if self.created_at_ns >= self.expires_at_ns:
                raise ValueError("planning selection exceeds observation expiry")
            if self.completed_depth < 1 or self.selected_action_id not in {
                item.action_id for item in self.action_values
            }:
                raise ValueError(
                    "planning selection requires a completed legal action value"
                )
        elif self.selected_action_id is not None:
            raise ValueError("planning fallback must not select an action")
        if self.probability_semantics == "unknown" and self.action_values:
            raise ValueError("unknown planning result must not invent action values")
        baseline_weights = None
        for action in self.action_values:
            if self.probability_semantics == "calibrated":
                if action.expected_quality is None or any(
                    item.belief_weight is None for item in action.hypothesis_values
                ):
                    raise ValueError("calibrated planning requires world probabilities")
                weights = {
                    item.hypothesis_id: float(item.belief_weight)
                    for item in action.hypothesis_values
                    if item.belief_weight is not None
                }
                if not math.isclose(
                    math.fsum(weights.values()) + self.residual_unknown_mass,
                    1.0,
                    rel_tol=0,
                    abs_tol=1e-10,
                ):
                    raise ValueError("planning world mass plus residual must equal one")
                point = math.fsum(
                    cast(float, item.belief_weight) * item.expected_quality
                    for item in action.hypothesis_values
                )
                if (
                    not math.isclose(
                        point, action.expected_quality, rel_tol=0, abs_tol=1e-10
                    )
                    or action.trace_unknown_mass + 1e-10 < self.residual_unknown_mass
                ):
                    raise ValueError(
                        "planning global value or residual contradicts hypotheses"
                    )
            else:
                if self.residual_unknown_mass != 1.0 or any(
                    item.relative_score is None for item in action.hypothesis_values
                ):
                    raise ValueError(
                        "conditional planning must retain unknown world probability"
                    )
                weights = {
                    item.hypothesis_id: cast(float, item.relative_score)
                    for item in action.hypothesis_values
                }
            if baseline_weights is not None and baseline_weights != weights:
                raise ValueError(
                    "planning actions must evaluate the same hypothesis set"
                )
            baseline_weights = weights
        if self.probability_semantics != "calibrated" and any(
            item.expected_quality is not None
            or item.outcomes
            or item.trace_unknown_mass != 1.0
            or item.lower_bound != -1.0
            or item.upper_bound != 1.0
            for item in self.action_values
        ):
            raise ValueError("uncalibrated planning must retain unknown global value")


# {
#   責務: [_trace: leaf質量とunknownの和1および有界tupleを検証する]
#   処理: [既知の契約だけを受け付け、不整合な値を公開前に拒否する]
#   引数: [対象の型付き値またはJSON投影] 戻り値: [検証済み値。constructor検証はなし]
#   エラー: [ValueError/TypeError: 数値・field・版・出所の契約違反]
# }
def _trace(values: tuple[WeightedValue, ...], unknown: float) -> None:
    belief_number(unknown, 1.0)
    if (
        type(values) is not tuple
        or len(values) > 256
        or any(not isinstance(item, WeightedValue) for item in values)
    ):
        raise ValueError("planning leaf trace exceeds contract")
    if not math.isclose(
        math.fsum(item.probability for item in values) + unknown,
        1.0,
        rel_tol=0,
        abs_tol=1e-10,
    ):
        raise ValueError("planning trace mass plus unknown must equal one")
