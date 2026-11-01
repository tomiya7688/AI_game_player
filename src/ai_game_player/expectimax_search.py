"""制限worker内で使うpure Expectimax。同期CoreだけではOS資源のhard limitを保証しない。"""

from collections import OrderedDict
from dataclasses import asdict, dataclass
import math
from itertools import product
import time
from typing import Generic, Iterator, cast

from ai_game_player.belief_contract import BeliefWeightKind
from ai_game_player.belief_validation import belief_id, belief_json, belief_number
from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.planning_contract import (
    PlanningDomain,
    PlanningRegistration,
    SearchBudget,
    StateT,
)
from ai_game_player.planning_result import (
    HypothesisActionValue,
    PlanningActionValue,
    PlanningCounters,
    WeightedValue,
)
from ai_game_player.transition_model import ActionTransition, ChanceOutcome
from ai_game_player.world_state_hypothesis import TypedStateRef


# {
#   責務: [SearchFailure: 不正な登録model/quality結果を固定codeで拒否する]
#   フィールド: [なし: messageは入力を含まない固定reason]
# }
class SearchFailure(ValueError):
    pass


# {
#   責務: [SearchCutoff: node/協調time予算で未完了深さを破棄する]
#   フィールド: [なし: 完了済みcheckpointの採用は親の受領deadline/鮮度が決める]
# }
class SearchCutoff(RuntimeError):
    pass


# {
#   責務: [SequenceInfeasible: 全支持worldで実行できない共通将来操作列だけを候補から除く]
#   フィールド: [ABIまたは計測用の固定契約。外部入力から型を生成しない]
# }
class SequenceInfeasible(RuntimeError):
    """One fixed open-loop continuation is not legal in every supported future."""


# {
#   責務: [SearchHypothesis: workerが復号・再検証したroot worldを保持する]
#   フィールド: [hypothesis_id: 元候補ID, state_ref: 型付き不変状態, weight: 校正確率または比較score]
# }
@dataclass(frozen=True, slots=True)
class SearchHypothesis(Generic[StateT]):
    hypothesis_id: str
    state_ref: TypedStateRef[StateT]
    weight: float

    # {
    #   責務: [__post_init__: 仮説ID・有限weight・型付き不変stateを検証する]
    #   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def __post_init__(self) -> None:
        belief_id(self.hypothesis_id)
        belief_number(self.weight)
        if not isinstance(self.state_ref, TypedStateRef):
            raise ValueError("search hypothesis requires a typed immutable state")


# {
#   責務: [NodeValue: 仮想未来policyの点評価・品質区間・有界leaf分布を保持する]
#   フィールド: [value/lower/upper: normalized quality, outcomes: 条件付きchance質量,
#     unknown: traceで未保持の質量。quality支持不足は各leaf区間で別表現する]
# }
@dataclass(frozen=True, slots=True)
class NodeValue:
    value: float
    lower: float
    upper: float
    outcomes: tuple[WeightedValue, ...]
    unknown: float


# {
#   責務: [TraceAccumulator: 分岐数と独立に有限個のleaf値だけを保持する]
#   フィールド: [maximum: 異なる値/区間の保持上限, values: 同値coalesceした質量]
# }
class TraceAccumulator:
    # {
    #   責務: [__init__: 最大bin数と値・支持区間ごとの確率質量を初期化する]
    #   引数: [maximum] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.values: dict[tuple[float, float, float], float] = {}

    # {
    #   責務: [add: 同値質量を合算し上限時は近傍binへ保守的区間で圧縮する]
    #   引数: [outcomes, multiplier] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def add(self, outcomes: tuple[WeightedValue, ...], multiplier: float) -> None:
        for item in outcomes:
            key = (
                item.value,
                cast(float, item.lower_bound),
                cast(float, item.upper_bound),
            )
            probability = item.probability * multiplier
            if probability <= 0:
                continue
            if key in self.values:
                self.values[key] = math.fsum((self.values[key], probability))
            elif len(self.values) < self.maximum:
                self.values[key] = probability
            else:
                nearest = min(
                    self.values,
                    key=lambda existing: (abs(existing[0] - item.value), existing),
                )
                old_mass = self.values.pop(nearest)
                total = math.fsum((old_mass, probability))
                # 比率を個別に丸めて足すと合計が1を超えるため、等しい点を保つ凸補間にする。
                if old_mass >= probability:
                    point = nearest[0] + (item.value - nearest[0]) * (
                        probability / total
                    )
                else:
                    point = item.value + (nearest[0] - item.value) * (old_mass / total)
                # 数学的平均は点の凸包内にある。桁落ち・極端な比率の丸めだけをその範囲へ戻す。
                point = min(
                    max(nearest[0], item.value), max(min(nearest[0], item.value), point)
                )
                merged = (
                    point,
                    min(nearest[1], cast(float, item.lower_bound)),
                    max(nearest[2], cast(float, item.upper_bound)),
                )
                self.values[merged] = math.fsum((self.values.get(merged, 0.0), total))

    # {
    #   責務: [finish: 有界leaf分布と保持外の残余質量を返す]
    #   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def finish(self) -> tuple[tuple[WeightedValue, ...], float]:
        result = tuple(
            WeightedValue(value, min(1.0, probability), lower, upper)
            for (value, lower, upper), probability in sorted(self.values.items())
            if probability > 0
        )
        return result, max(0.0, 1.0 - math.fsum(item.probability for item in result))


# {
#   責務: [ExpectimaxSearch: 完全なroot深さだけを公開するゲーム非依存の反復深化探索]
#   フィールド: [domain/registration: trusted model/codec/qualityと版, budget: 有限予算,
#     hypotheses/allowed: 初期worldとverified抽象Action集合, kind/residual: 確率の意味,
#     _cache/_cache_bytes: request単位のLRU transposition cache, _started: 子の独立時計,
#     _nodes/_branches/_leaves/_hits/_peak_cache: 有限な実行カウンタ]
# }
class ExpectimaxSearch(Generic[StateT]):
    # {
    #   責務: [__init__: scope・正の支持world・校正質量を検査しrequest単位cacheを作る]
    #   引数: [domain, registration, budget, hypotheses, allowed, kind, residual] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def __init__(
        self,
        domain: PlanningDomain[StateT],
        registration: PlanningRegistration,
        budget: SearchBudget,
        hypotheses: tuple[SearchHypothesis[StateT], ...],
        allowed: tuple[str, ...],
        kind: BeliefWeightKind,
        residual: float,
    ) -> None:
        self.domain, self.registration, self.budget = domain, registration, budget
        if (
            type(hypotheses) is not tuple
            or len(hypotheses) > 256
            or any(not isinstance(item, SearchHypothesis) for item in hypotheses)
        ):
            raise ValueError("search requires a bounded typed hypothesis tuple")
        if domain.future_state_observable != registration.future_state_observable:
            raise ValueError("planning observability registration mismatch")
        self.hypotheses = tuple(item for item in hypotheses if item.weight > 0)
        self.allowed, self.kind, self.residual = (
            allowed,
            kind,
            belief_number(residual, 1.0),
        )
        if (
            type(allowed) is not tuple
            or not 1 <= len(allowed) <= budget.max_actions
            or len(set(allowed)) != len(allowed)
        ):
            raise ValueError("search requires bounded unique action IDs")
        for action in allowed:
            belief_id(action)
        if len({item.hypothesis_id for item in hypotheses}) != len(hypotheses) or any(
            item.state_ref.state_type is not domain.state_type
            or item.state_ref.game_id != registration.game_id
            or item.state_ref.state_schema_id != registration.state_schema_id
            for item in hypotheses
        ):
            raise ValueError("search hypothesis identity or type mismatch")
        if kind == BeliefWeightKind.CALIBRATED_PROBABILITY:
            for item in hypotheses:
                belief_number(item.weight, 1.0)
            if not math.isclose(
                math.fsum(item.weight for item in hypotheses) + residual,
                1.0,
                rel_tol=0,
                abs_tol=1e-10,
            ):
                raise ValueError("search world mass plus residual must equal one")
        elif kind != BeliefWeightKind.RELATIVE_SCORE or residual != 1.0:
            raise ValueError(
                "search requires calibrated or explicit conditional probability semantics"
            )
        self._started = time.monotonic_ns()
        self._nodes = self._branches = self._leaves = self._hits = self._peak_cache = 0
        self._cache_bytes = 0
        self._cache: OrderedDict[
            tuple[str, int | tuple[str, ...]], tuple[NodeValue, int]
        ] = OrderedDict()

    # {
    #   責務: [counters: 採用候補の完全深さ時点の累積実行量を返す]
    #   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def counters(self) -> PlanningCounters:
        return PlanningCounters(
            self._nodes,
            self._branches,
            self._leaves,
            self._hits,
            self._peak_cache,
            self._cache_bytes,
            time.monotonic_ns() - self._started,
        )

    # {
    #   責務: [deepen: 共通合法rootを全て評価できた深さだけを順次公開する]
    #   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def deepen(
        self,
    ) -> Iterator[tuple[int, tuple[PlanningActionValue, ...], PlanningCounters]]:
        if not self.hypotheses:
            raise SearchFailure("no_belief_candidates")
        common = set(self.allowed)
        for item in self.hypotheses:
            self._check()
            common.intersection_update(self._legal(item.state_ref.state))
        if not common:
            raise SearchFailure("no_common_legal_action")
        actions = tuple(sorted(common))
        for depth in range(1, self.budget.max_depth + 1):
            values = tuple(
                self._root_action(action, depth)
                if self.domain.future_state_observable
                else self._open_loop_root(action, depth)
                for action in actions
            )
            self._check()
            yield depth, values, self.counters()

    # {
    #   責務: [_open_loop_root: 全worldとchanceへ共通の将来操作列を列挙し同一policyで比較する]
    #   引数: [action, depth] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _open_loop_root(self, action: str, depth: int) -> PlanningActionValue:
        best = None
        for tail in product(self.allowed, repeat=depth - 1):
            self._check()
            try:
                value = self._root_action(action, depth, (action, *tail))
            except SequenceInfeasible:
                continue
            if best is None or value.ranking_score > best.ranking_score:
                best = value
        if best is None:
            raise SearchCutoff("no_common_future_sequence")
        return best

    # {
    #   責務: [_sequence: 固定将来操作列を状態に適用し実行不能な分岐を拒否する]
    #   引数: [state, sequence] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _sequence(
        self, state: TypedStateRef[StateT], sequence: tuple[str, ...]
    ) -> NodeValue:
        self._visit()
        key = (state.digest, sequence)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            self._cache.move_to_end(key)
            return cached[0]
        if not sequence:
            result = self._leaf(state.state)
        else:
            legal = self._legal(state.state)
            if not legal:
                result = self._leaf(state.state)
            elif sequence[0] not in legal:
                raise SequenceInfeasible("future action is not common to every world")
            else:
                result = self._action(state, sequence[0], len(sequence), sequence[1:])
        self._cache_put(key, result)
        return result

    # {
    #   責務: [_root_action: 仮説条件付きchance値と校正済みglobal質量を区別して集計する]
    #   引数: [action, depth, sequence] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _root_action(
        self, action: str, depth: int, sequence: tuple[str, ...] | None = None
    ) -> PlanningActionValue:
        conditional = []
        point = lower = upper = 0.0
        rank_values = []
        trace = TraceAccumulator(self.budget.max_trace_values)
        calibrated = self.kind == BeliefWeightKind.CALIBRATED_PROBABILITY
        maximum = max(item.weight for item in self.hypotheses)
        scaled_total = (
            math.fsum(item.weight / maximum for item in self.hypotheses)
            if maximum > 0
            else 1.0
        )
        for item in self.hypotheses:
            node = (
                self._action(item.state_ref, action, depth)
                if sequence is None
                else self._sequence(item.state_ref, sequence)
            )
            conditional.append(
                HypothesisActionValue(
                    item.hypothesis_id,
                    item.weight if calibrated else None,
                    None if calibrated else item.weight,
                    node.value,
                    node.lower,
                    node.upper,
                    node.outcomes,
                    node.unknown,
                )
            )
            ranking_weight = (
                item.weight
                if calibrated
                else (item.weight / maximum / scaled_total if maximum > 0 else 0.0)
            )
            rank_values.append(ranking_weight * node.value)
            if calibrated:
                point = math.fsum((point, item.weight * node.value))
                lower = math.fsum((lower, item.weight * node.lower))
                upper = math.fsum((upper, item.weight * node.upper))
                trace.add(node.outcomes, item.weight)
        outcomes, trace_unknown = trace.finish() if calibrated else ((), 1.0)
        return PlanningActionValue(
            action,
            _unit(point) if calibrated else None,
            _unit(lower - self.residual) if calibrated else -1.0,
            _unit(upper + self.residual) if calibrated else 1.0,
            outcomes,
            trace_unknown,
            tuple(conditional),
            _unit(math.fsum(rank_values)),
        )

    # {
    #   責務: [_max: 完全将来観測を保証するdomainだけで状態別の将来maxを計算する]
    #   引数: [state, remaining] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _max(self, state: TypedStateRef[StateT], remaining: int) -> NodeValue:
        self._visit()
        key = (state.digest, remaining)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            self._cache.move_to_end(key)
            return cached[0]
        if remaining == 0:
            result = self._leaf(state.state)
        else:
            legal = tuple(
                action for action in self._legal(state.state) if action in self.allowed
            )
            if not legal:
                result = self._leaf(state.state)
            else:
                choices = [
                    (self._action(state, action, remaining), action)
                    for action in sorted(legal)
                ]
                result = max(
                    choices,
                    key=lambda item: (item[0].value, -sorted(legal).index(item[1])),
                )[0]
        self._cache_put(key, result)
        return result

    # {
    #   責務: [_action: 登録modelの遷移とchance確率を検証し同じ継続policyで積分する]
    #   引数: [state, action, remaining, tail] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _action(
        self,
        state: TypedStateRef[StateT],
        action: str,
        remaining: int,
        tail: tuple[str, ...] | None = None,
    ) -> NodeValue:
        self._visit()
        try:
            transition = self.domain.model.apply_action(state.state, action)
            self._check()
            if (
                not isinstance(transition, ActionTransition)
                or type(transition.changed) is not bool
                or type(transition.reward_delta) not in (int, float)
                or not math.isfinite(transition.reward_delta)
            ):
                raise SearchFailure("invalid_action_transition")
            self._freeze(transition.state)
            outcomes = self.domain.model.chance_outcomes(transition)
            self._check()
            if (
                type(outcomes) is not tuple
                or not 1 <= len(outcomes) <= self.budget.max_chance_outcomes
            ):
                raise SearchFailure("chance_branch_budget_or_type")
            probabilities = []
            for item in outcomes:
                self._check()
                if (
                    not isinstance(item, ChanceOutcome)
                    or type(item.probability) not in (int, float)
                    or not 0 <= item.probability <= 1
                    or not math.isfinite(item.probability)
                ):
                    raise SearchFailure("invalid_chance_probability")
                probabilities.append(float(item.probability))
            if not math.isclose(
                math.fsum(probabilities), 1.0, rel_tol=0, abs_tol=1e-12
            ):
                raise SearchFailure("chance_mass_not_one")
            points, lowers, uppers = [], [], []
            trace = TraceAccumulator(self.budget.max_trace_values)
            for item, probability in zip(outcomes, probabilities):
                self._check()
                if probability == 0:
                    continue
                self._branches += 1
                reference = self._freeze(item.state)
                child = (
                    self._max(reference, remaining - 1)
                    if tail is None
                    else self._sequence(reference, tail)
                )
                points.append(probability * child.value)
                lowers.append(probability * child.lower)
                uppers.append(probability * child.upper)
                trace.add(child.outcomes, probability)
            values, unknown = trace.finish()
            return NodeValue(
                _unit(math.fsum(points)),
                _unit(math.fsum(lowers)),
                _unit(math.fsum(uppers)),
                values,
                unknown,
            )
        except (SearchCutoff, SearchFailure):
            raise
        except (ValueError, TypeError, OverflowError) as error:
            raise SearchFailure("invalid_transition_or_state") from error

    # {
    #   責務: [_leaf: 注入qualityの版と支持区間を確認しworld確率と独立に評価する]
    #   引数: [state] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _leaf(self, state: StateT) -> NodeValue:
        self._check()
        self._leaves += 1
        quality = self.domain.evaluator.evaluate(state)
        self._check()
        if (
            not isinstance(quality, NumericQuality)
            or quality.projection_version
            != self.registration.quality_projection_version
            or quality.profile_version != self.registration.quality_profile_version
        ):
            raise SearchFailure("invalid_quality_or_profile")
        lower, upper = quality.bounds
        value = quality.effective_value
        return NodeValue(
            value, lower, upper, (WeightedValue(value, 1.0, lower, upper),), 0.0
        )

    # {
    #   責務: [_legal: 有界unique操作IDだけをmodelの合法集合として受理する]
    #   引数: [state] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _legal(self, state: StateT) -> tuple[str, ...]:
        self._check()
        actions = self.domain.legal_actions(state)
        self._check()
        if (
            type(actions) is not tuple
            or len(actions) > self.budget.max_actions
            or len(set(actions)) != len(actions)
        ):
            raise SearchFailure("invalid_legal_action_set")
        for action in actions:
            belief_id(action)
        return actions

    # {
    #   責務: [_freeze: 仮想stateの型・不変性・canonical byte上限を再検証する]
    #   引数: [state] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _freeze(self, state: StateT) -> TypedStateRef[StateT]:
        self._check()
        reference = TypedStateRef(
            self.registration.game_id,
            self.registration.state_schema_id,
            self.domain.state_type,
            state,
        )
        self._check()
        if len(reference.canonical_bytes) > self.budget.max_state_bytes:
            raise SearchFailure("state_byte_budget")
        return reference

    # {
    #   責務: [_cache_put: logical byte数と件数の両上限を守りLRU cacheへ格納する]
    #   引数: [key, value] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _cache_put(
        self, key: tuple[str, int | tuple[str, ...]], value: NodeValue
    ) -> None:
        if not self.budget.max_cache_entries or not self.budget.max_cache_bytes:
            return
        size = len(belief_json({"key": key, "value": asdict(value)}))
        if size > self.budget.max_cache_bytes:
            return
        while self._cache and (
            len(self._cache) >= self.budget.max_cache_entries
            or self._cache_bytes + size > self.budget.max_cache_bytes
        ):
            _, (_, old_size) = self._cache.popitem(last=False)
            self._cache_bytes -= old_size
        self._cache[key] = (value, size)
        self._cache_bytes += size
        self._peak_cache = max(self._peak_cache, len(self._cache))

    # {
    #   責務: [_visit: 探索nodeの累積上限を超える前に未完了深さを打ち切る]
    #   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _visit(self) -> None:
        self._check()
        if self._nodes >= self.budget.max_nodes:
            raise SearchCutoff("node_budget")
        self._nodes += 1

    # {
    #   責務: [_check: worker独立時計で協調計算予算を検査する]
    #   引数: [なし] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
    # }
    def _check(self) -> None:
        if time.monotonic_ns() - self._started >= self.budget.wall_time_ms * 1_000_000:
            raise SearchCutoff("compute_budget")


# {
#   責務: [_unit: 浮動小数の端数だけを正規化quality区間へ丸める]
#   引数: [value] 戻り値: [宣言した型の結果。契約違反は呼出元境界へ通知]
# }
def _unit(value: float) -> float:
    return max(-1.0, min(1.0, value))
