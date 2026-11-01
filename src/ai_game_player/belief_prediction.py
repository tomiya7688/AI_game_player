"""有限の遷移分布を、観測事実ではないtyped world候補へ展開する。"""

from collections.abc import Callable
from hashlib import sha256
import math

from ai_game_player.belief_contract import (
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.belief_likelihood import (
    BeliefActionRef,
    BeliefEstimatorPolicy,
    checked_likelihood,
)
from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.transition_model import (
    ActionTransition,
    ChanceOutcome,
    TransitionModel,
)
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    StateT,
    TypedStateRef,
    WorldStateHypothesis,
)


# {
#   責務: [expand_prediction: 確定操作とchanceを分離して排他経路の確率だけを合算する]
#   処理: [一回の生成状態数/分岐を制限し、型/確率が不正な親や切詰めの質量は未知へ残す。
#     モデルは信頼された有限処理を前提とし、ノード境界で期限を検査する]
#   引数: [prior: 現在の不変分布, action: 実行参照, model: 任意のpure遷移,
#     model_id/model_version: 出所, policy/max_input: 展開/Storeの上限, check_deadline: 期限検査]
#   戻り値: [候補tuple, 検査した分岐数, 不保持分岐数]
# }
def expand_prediction(
    prior: BeliefSnapshot[StateT],
    action: BeliefActionRef,
    model: TransitionModel[StateT],
    model_id: str,
    model_version: str,
    policy: BeliefEstimatorPolicy,
    max_input: int,
    max_state_bytes: int,
    check_deadline: Callable[[], None],
) -> tuple[tuple[WorldStateHypothesis[StateT], ...], int, int]:
    if not prior.candidates:
        return (), 0, 0
    maximum = min(policy.max_generated_states, max_input)
    worlds: dict[str, tuple[TypedStateRef[StateT], float, float]] = {}
    branches = dropped = 0
    retained_bytes = 0
    probabilities = (
        tuple(item.weight.value for item in prior.candidates)
        if prior.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY
        else prior.ranking_weights
    )
    for parent, weight in zip(prior.candidates, probabilities):
        check_deadline()
        try:
            transition = model.apply_action(parent.state_ref.state, action.action_id)
            check_deadline()
            if (
                not isinstance(transition, ActionTransition)
                or type(transition.state) is not parent.state_ref.state_type
                or type(transition.changed) is not bool
            ):
                raise ValueError("model returned an invalid action transition")
            outcomes = model.chance_outcomes(transition)
            check_deadline()
            if (
                type(outcomes) is not tuple
                or not 1 <= len(outcomes) <= policy.max_transition_outcomes
            ):
                raise ValueError("model chance expansion exceeds its contract")
            masses = []
            for outcome in outcomes:
                check_deadline()
                if not isinstance(outcome, ChanceOutcome):
                    raise ValueError("model chance outcome has an invalid type")
                masses.append(checked_likelihood(outcome.probability))
            if not math.isclose(math.fsum(masses), 1.0, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("model chance probability mass does not equal one")
            # Validate the complete parent branch before adding any of its mass.
            states = []
            pending_bytes = 0
            for outcome in outcomes:
                check_deadline()
                state_ref = TypedStateRef(
                    parent.state_ref.game_id,
                    parent.state_ref.state_schema_id,
                    parent.state_ref.state_type,
                    outcome.state,
                )
                check_deadline()
                size = len(state_ref.canonical_bytes)
                if (
                    size > max_state_bytes
                    or retained_bytes + pending_bytes + size
                    > policy.max_expansion_bytes
                ):
                    raise ValueError("prediction canonical state byte budget exceeded")
                states.append(state_ref)
                pending_bytes += size
        except ValueError:
            # A failed model branch is unknown, never silently a legal/no-op action.
            dropped += 1
            continue
        for state_ref, chance in zip(states, masses):
            check_deadline()
            branches += 1
            probability = weight * chance
            if probability == 0:
                continue
            existing = worlds.get(state_ref.digest)
            if existing is None:
                if len(worlds) >= maximum:
                    dropped += 1
                    continue
                worlds[state_ref.digest] = (state_ref, probability, 0.0)
                retained_bytes += len(state_ref.canonical_bytes)
            else:
                # Kahan summation bounds accumulator memory independently of path count.
                adjusted = probability - existing[2]
                total = existing[1] + adjusted
                correction = (total - existing[1]) - adjusted
                worlds[state_ref.digest] = (existing[0], total, correction)
    # Prior evidence time/TTL are retained; prediction time is not a new observation.
    observed = min(item.freshness.observed_at_ns for item in prior.candidates)
    expires = min(
        prior.expires_at_ns,
        *(item.freshness.expires_at_ns for item in prior.candidates),
    )
    freshness = BeliefFreshness(observed, expires)
    reference = (
        "transition."
        + sha256(
            (
                action.receipt_ref
                + ":"
                + model_id
                + ":"
                + model_version
                + ":"
                + str(prior.hypothesis_set_version)
            ).encode()
        ).hexdigest()
    )
    evidence = BeliefEvidenceRef(
        reference,
        reference,
        BeliefEvidenceKind.PREDICTION,
        model_id,
        model_version,
        prior.context,
        observed,
    )
    calibration = prior.candidates[0].weight.calibration_id
    candidates = tuple(
        WorldStateHypothesis(
            "prediction." + digest,
            state_ref,
            prior.context,
            BeliefWeight(prior.weight_kind, min(1.0, mass), calibration),
            HypothesisOrigin.PREDICTION,
            (evidence,),
            freshness,
            prior_action_ref=action.receipt_ref,
            predicted_outcome_ref=reference,
        )
        for digest, (state_ref, mass, _) in sorted(worlds.items())
    )
    return candidates, branches, dropped
