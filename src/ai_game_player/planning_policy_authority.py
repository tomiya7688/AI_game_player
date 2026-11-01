"""Planningの現在authority・価値尺度・hard候補境界をsoft policyの前に検証する。"""

from math import isclose
from typing import Any

from ai_game_player.belief_contract import BeliefContext, BeliefWeightKind
from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.belief_validation import belief_id
from ai_game_player.planning_information import PlanningObservationModel
from ai_game_player.planning_policy_contract import PlanningComputationEstimate
from ai_game_player.planning_result import PlanningResult


# {
#   責務: [planning_allowed_actions: 現在の候補と不可侵vetoを数値評価の前に適用する]
#   処理: [最大32の不変ID集合を検証し、vetoを常に優先する]
#   引数: [allowed/veto: trusted verifier/safety adapterの現在集合]
#   戻り値: [frozenset: 提案可能なID]
#   エラー: [ValueError: 不正な集合]
# }
def planning_allowed_actions(
    allowed: tuple[str, ...], veto: tuple[str, ...]
) -> frozenset[str]:
    for values in (allowed, veto):
        if (
            type(values) is not tuple
            or len(values) > 32
            or len(set(values)) != len(values)
        ):
            raise ValueError("planning policy requires bounded candidate IDs")
        for value in values:
            belief_id(value)
    return frozenset(allowed) - frozenset(veto)


# {
#   責務: [validate_planning_source: 探索を現在のBelief/実行世代/時刻へ完全に結び付ける]
#   処理: [期限後受領、別snapshot/版、未校正から確率への偽変換を拒否する]
#   引数: [result/belief: 元探索, context/run_generation/now: 現authority]
#   戻り値: [なし]
#   エラー: [ValueError: 古い/別対象/意味不一致]
# }
def validate_planning_source(
    result: PlanningResult,
    belief: BeliefSnapshot[Any],
    context: BeliefContext,
    run_generation: int,
    now: int,
) -> None:
    if (
        type(result) is not PlanningResult
        or result.context != context
        or belief.context != context
        or result.belief_version != belief.hypothesis_set_version
        or result.run_generation != run_generation
        or not result.created_at_ns <= now < result.expires_at_ns
        or not belief.created_at_ns <= now < belief.expires_at_ns
        or result.created_at_ns > result.compute_cutoff_at_ns
        or result.quality_projection_version != 1
        or result.value_unit != "normalized_quality.v1"
        or not isclose(
            result.residual_unknown_mass,
            belief.residual_unknown_mass,
            rel_tol=0,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("planning source context or time mismatch")
    continuation = getattr(result, "continuation_semantics", "one_step")
    if continuation not in (
        "one_step",
        "open_loop_mpc",
        "state_observed_expectimax",
    ) or (result.completed_depth > 1 and continuation == "one_step"):
        raise ValueError(
            "deep planning requires an explicit feasible continuation policy"
        )
    calibrated = belief.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY
    if (result.probability_semantics == "calibrated") != calibrated:
        raise ValueError("planning probability semantics mismatch")


# {
#   責務: [validate_planning_estimate: 観測/計算見積の対象と価値尺度を探索へ照合する]
#   処理: [別Frame/Belief版/評価器尺度のgain/costを利用しない]
#   引数: [estimate: 出所付き見積, result: 対象探索]
#   戻り値: [なし]
#   エラー: [ValueError: 対象または尺度不一致]
# }
def validate_planning_estimate(
    estimate: PlanningObservationModel | PlanningComputationEstimate,
    result: PlanningResult,
) -> None:
    if (
        type(estimate) not in (PlanningObservationModel, PlanningComputationEstimate)
        or estimate.context != result.context
        or estimate.belief_version != result.belief_version
        or estimate.evaluator_id != result.evaluator_id
        or estimate.evaluator_version != result.evaluator_version
        or estimate.value_unit != result.value_unit
        or estimate.quality_profile_version != result.quality_profile_version
    ):
        raise ValueError("planning information estimate source mismatch")
