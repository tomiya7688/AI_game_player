"""探索契約をRisk/情報価値へ投影し、world確率とquality支持を混同しない。"""

from math import isclose
from typing import Any

from ai_game_player.belief_contract import BeliefWeightKind
from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.planning_policy_contract import (
    PlanningActionRisk,
    PlanningRiskPolicyConfig,
)
from ai_game_player.planning_result import PlanningActionValue, WeightedValue
from ai_game_player.planning_risk import lower_tail_risk, planning_value


# {
#   責務: [project_action_risk: interval leafの両端で下側riskを包絡する]
#   処理: [未知は下限計算で-1、上限計算で+1へ置く。confidenceを確率に乗算しない]
#   引数: [action: 565探索値, config: tail/soft risk設定, conditional: 単一world条件付きの明示実験]
#   戻り値: [PlanningActionRisk: 条件に応じたglobalまたはconditional risk]
#   エラー: [ValueError: 不正分布]
# }
def project_action_risk(
    action: PlanningActionValue,
    config: PlanningRiskPolicyConfig,
    *,
    conditional: bool = False,
) -> PlanningActionRisk:
    if conditional:
        source = action.hypothesis_values[0]
        values = source.conditional_outcomes
        unknown = source.trace_unknown_mass
        source_lower, source_upper = source.lower_bound, source.upper_bound
    else:
        values = action.outcomes
        unknown = action.trace_unknown_mass
        source_lower, source_upper = action.lower_bound, action.upper_bound
    for leaf in values:
        if type(leaf) is not WeightedValue:
            raise ValueError("planning risk requires typed leaves")
    lower = lower_tail_risk(
        tuple(
            (
                planning_value(
                    leaf.lower_bound if leaf.lower_bound is not None else leaf.value
                ),
                leaf.probability,
            )
            for leaf in values
        ),
        unknown,
        alpha=config.tail_alpha,
        catastrophic_value=config.catastrophic_value,
    )
    upper_atoms = tuple(
        (
            planning_value(
                leaf.upper_bound if leaf.upper_bound is not None else leaf.value
            ),
            leaf.probability,
        )
        for leaf in values
    )
    if unknown:
        upper_atoms += ((1.0, unknown),)
    upper = lower_tail_risk(
        upper_atoms,
        0,
        alpha=config.tail_alpha,
        catastrophic_value=config.catastrophic_value,
    )
    expected_lower = max(source_lower, lower.expected_lower)
    expected_upper = min(source_upper, upper.expected_upper)
    if expected_lower > expected_upper + 1e-10:
        raise ValueError("planning trace and action value bounds disagree")
    score = (
        1 - config.risk_aversion
    ) * expected_lower + config.risk_aversion * lower.lower_cvar
    return PlanningActionRisk(
        action.action_id,
        expected_lower,
        expected_upper,
        lower.lower_cvar,
        upper.lower_cvar,
        lower.lower_quantile,
        lower.worst_value,
        lower.catastrophic_probability_upper,
        unknown,
        score,
        lower.lower_cvar >= config.min_cvar
        and lower.catastrophic_probability_upper <= config.max_catastrophe,
    )


# {
#   責務: [project_world_values: 同じBelief候補のaction区間を整合した行列にする]
#   処理: [欠落world・別weight・未校正からglobal確率への偽変換を拒否する]
#   引数: [belief: 元の不変集合, actions: 現在許可される探索値, conditional: 単一仮説条件付き実験]
#   戻り値: [tuple: hypothesis IDs、絶対weight、world×action区間、最善手の一致分類]
#   エラー: [ValueError: 候補/重み/出所不整合]
# }
def project_world_values(
    belief: BeliefSnapshot[Any],
    actions: tuple[PlanningActionValue, ...],
    *,
    conditional: bool = False,
) -> tuple[
    tuple[str, ...], tuple[float, ...], tuple[tuple[tuple[float, float], ...], ...], str
]:
    ids = tuple(item.hypothesis_id for item in belief.candidates)
    if not ids or not actions:
        raise ValueError("planning policy requires represented worlds and actions")
    maps = [
        {item.hypothesis_id: item for item in action.hypothesis_values}
        for action in actions
    ]
    if any(set(mapped) != set(ids) for mapped in maps):
        raise ValueError("planning policy hypothesis coverage differs from belief")
    rows = []
    possible_actions = []
    for candidate in belief.candidates:
        row = []
        for mapped in maps:
            value = mapped[candidate.hypothesis_id]
            if belief.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY:
                if (
                    value.relative_score is not None
                    or value.belief_weight is None
                    or not isclose(
                        value.belief_weight,
                        candidate.weight.value,
                        rel_tol=0,
                        abs_tol=1e-12,
                    )
                ):
                    raise ValueError("planning policy hypothesis probability mismatch")
            elif (
                value.belief_weight is not None
                or value.relative_score != candidate.weight.value
            ):
                raise ValueError("planning policy relative score is not a probability")
            row.append((value.lower_bound, value.upper_bound))
        immutable_row = tuple(row)
        rows.append(immutable_row)
        maximum_lower = max(pair[0] for pair in immutable_row)
        if conditional or candidate.weight.value > 0:
            possible_actions.append(
                {
                    action.action_id
                    for action, pair in zip(actions, immutable_row)
                    if pair[1] >= maximum_lower - 1e-12
                }
            )
    common = set.intersection(*possible_actions) if possible_actions else set()
    if not possible_actions:
        disagreement = "unknown"
    elif all(len(items) == 1 for items in possible_actions) and len(common) == 1:
        disagreement = "agreement"
    elif not common:
        disagreement = "disagreement"
    else:
        disagreement = "unknown"
    weights = (
        (1.0,)
        if conditional
        else tuple(item.weight.value for item in belief.candidates)
    )
    return ids, weights, tuple(rows), disagreement
