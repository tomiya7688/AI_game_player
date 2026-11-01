"""観測後にも同じrisk policyで実際に採択できる操作の情報価値を保守評価する。"""

from collections.abc import Callable
from dataclasses import dataclass
from math import fsum
from typing import Any

from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.planning_information import PlanningObservationModel
from ai_game_player.planning_policy_contract import (
    PlanningActionRisk,
    PlanningRiskPolicyConfig,
)
from ai_game_player.planning_policy_projection import project_action_risk
from ai_game_player.planning_result import PlanningActionValue, WeightedValue


# {
#   責務: [PlanningObservationPolicyGain: 観測後mappingの改善と全体riskを一対で保持する]
#   フィールド: [lower/upper: expected qualityの改善区間, contingent_risk: signal×world×chanceの合成risk]
# }
@dataclass(frozen=True, slots=True)
class PlanningObservationPolicyGain:
    lower: float
    upper: float
    contingent_risk: PlanningActionRisk


# {
#   責務: [observation_policy_gain: risk-neutral EVSIと実risk policyの採択可能な改善を区別する]
#   処理: [各signal後のchance分布へ同じsoft risk条件を再適用し、選ぶ操作の期待利益を比較する。
#     未知worldのchannel不足や条件付きtrace総数超過では利益未確定とする。
#     signal後ごとの合格だけでは足りないため、mapping全体の混合分布にもrisk条件を再適用する]
#   引数: [belief/actions: 同じworld/action表, model: 校正済み観測channelと任意の保留価値,
#     current_risks: 観測前policy結果, config: 同じrisk条件, check_deadline: policy内の期限検査]
#   戻り値: [PlanningObservationPolicyGainまたはNone: 改善区間とmapping全体risk。未確定はNone]
# }
def observation_policy_gain(
    belief: BeliefSnapshot[Any],
    actions: tuple[PlanningActionValue, ...],
    model: PlanningObservationModel,
    current_risks: tuple[PlanningActionRisk, ...],
    config: PlanningRiskPolicyConfig,
    check_deadline: Callable[[], int],
) -> PlanningObservationPolicyGain | None:
    if (
        belief.residual_unknown_mass != 0
        or sum(
            len(h.conditional_outcomes)
            for action in actions
            for h in action.hypothesis_values
        )
        > config.max_trace_atoms
    ):
        return None
    feasible = [item for item in current_risks if item.acceptable]
    if feasible:
        current = min(feasible, key=lambda item: (-item.score, item.action_id))
        before_lower, before_upper = current.expected_lower, current.expected_upper
    elif model.withhold_value is not None:
        before_lower = before_upper = model.withhold_value
    else:
        return None
    represented = tuple(
        candidate for candidate in belief.candidates if candidate.weight.value > 0
    )
    conditional_maps = [
        {item.hypothesis_id: item for item in action.hypothesis_values}
        for action in actions
    ]
    after_lower = []
    after_upper = []
    contingent: dict[tuple[float, float | None, float | None], float] = {}
    after_expected = []
    for signal in model.signal_likelihoods:
        check_deadline()
        masses = tuple(
            candidate.weight.value * q for candidate, q in zip(represented, signal)
        )
        signal_probability = fsum(masses)
        if signal_probability == 0:
            continue
        posterior = tuple(mass / signal_probability for mass in masses)
        candidates = []
        for action, values in zip(actions, conditional_maps):
            check_deadline()
            by_world = tuple(
                values[candidate.hypothesis_id] for candidate in represented
            )
            coalesced: dict[tuple[float, float | None, float | None], float] = {}
            for weight, world in zip(posterior, by_world):
                if weight == 0:
                    continue
                for leaf in world.conditional_outcomes:
                    key = (leaf.value, leaf.lower_bound, leaf.upper_bound)
                    if key in coalesced or len(coalesced) < 256:
                        coalesced[key] = (
                            coalesced.get(key, 0.0) + weight * leaf.probability
                        )
            outcomes = tuple(
                WeightedValue(key[0], mass, key[1], key[2])
                for key, mass in coalesced.items()
                if mass > 0
            )
            unknown = max(0.0, 1 - fsum(leaf.probability for leaf in outcomes))
            lower = fsum(
                weight * world.lower_bound for weight, world in zip(posterior, by_world)
            )
            upper = fsum(
                weight * world.upper_bound for weight, world in zip(posterior, by_world)
            )
            expected = fsum(
                weight * world.expected_quality
                for weight, world in zip(posterior, by_world)
            )
            projected = PlanningActionValue(
                action.action_id,
                expected,
                lower,
                upper,
                outcomes,
                unknown,
                action.hypothesis_values,
                expected,
            )
            risk = project_action_risk(projected, config)
            if risk.acceptable:
                candidates.append((risk, projected))
        if candidates:
            selected, selected_action = min(
                candidates, key=lambda item: (-item[0].score, item[0].action_id)
            )
            lower, upper = selected.expected_lower, selected.expected_upper
            selected_outcomes = selected_action.outcomes
            if selected_action.expected_quality is None:
                raise ValueError("contingent policy lost calibrated expected value")
            expected = selected_action.expected_quality
        elif model.withhold_value is not None:
            lower = upper = expected = model.withhold_value
            selected_outcomes = (WeightedValue(expected, 1.0),)
        else:
            lower, upper, expected = -1.0, 1.0, 0.0
            selected_outcomes = ()
        for leaf in selected_outcomes:
            key = (leaf.value, leaf.lower_bound, leaf.upper_bound)
            if key in contingent or len(contingent) < 256:
                contingent[key] = (
                    contingent.get(key, 0.0) + signal_probability * leaf.probability
                )
        after_expected.append(signal_probability * expected)
        after_lower.append(signal_probability * lower)
        after_upper.append(signal_probability * upper)
    check_deadline()
    outcomes = tuple(
        WeightedValue(key[0], mass, key[1], key[2])
        for key, mass in contingent.items()
        if mass > 0
    )
    unknown = max(0.0, 1 - fsum(leaf.probability for leaf in outcomes))
    mapping = PlanningActionValue(
        "observation.mapping",
        fsum(after_expected),
        fsum(after_lower),
        fsum(after_upper),
        outcomes,
        unknown,
        (),
        fsum(after_expected),
    )
    risk = project_action_risk(mapping, config)
    return PlanningObservationPolicyGain(
        max(-2.0, fsum(after_lower) - before_upper),
        min(2.0, fsum(after_upper) - before_lower),
        risk,
    )
