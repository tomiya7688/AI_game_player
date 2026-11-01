"""PolicyのCPU検証用合成world/channel。実認識・実プレイ確率とは独立した参照fixture。"""

from dataclasses import dataclass
from math import fsum

from ai_game_player.belief_contract import (
    BeliefContext,
    BeliefEvidenceKind,
    BeliefEvidenceRef,
    BeliefFreshness,
    BeliefWeight,
    BeliefWeightKind,
)
from ai_game_player.belief_snapshot import BeliefDiagnostics, BeliefSnapshot
from ai_game_player.world_state_hypothesis import (
    HypothesisOrigin,
    TypedStateRef,
    WorldStateHypothesis,
)
from ai_game_player.planning_information import PlanningObservationModel
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_result import (
    HypothesisActionValue,
    PlanningActionValue,
    PlanningCounters,
    PlanningResult,
    WeightedValue,
)


@dataclass(frozen=True)
class CardWorld:
    hidden_card: int


def make_belief(weights=(0.5, 0.5), *, seq=1, score=False):
    context = BeliefContext(
        "cards",
        "table",
        "session",
        f"snapshot{seq}",
        (f"frame{seq}",),
        seq,
        1,
        3,
        "capture",
        1,
        "clock",
    )
    kind = (
        BeliefWeightKind.RELATIVE_SCORE
        if score
        else BeliefWeightKind.CALIBRATED_PROBABILITY
    )
    candidates = []
    for index, weight in enumerate(weights):
        evidence = BeliefEvidenceRef(
            f"evidence{index}",
            f"correlation{index}",
            BeliefEvidenceKind.OBSERVATION,
            "recognizer",
            "v1",
            context,
            10,
            context.frame_ids[0],
        )
        candidates.append(
            WorldStateHypothesis(
                f"h{index}",
                TypedStateRef("cards", "card.v1", CardWorld, CardWorld(index)),
                context,
                BeliefWeight(kind, weight, None if score else "calibration.v1"),
                HypothesisOrigin.OBSERVATION_HYPOTHESIS,
                (evidence,),
                BeliefFreshness(10, 10**9),
            )
        )
    return BeliefSnapshot(
        seq,
        context,
        tuple(candidates),
        kind,
        1 if score else 1 - fsum(weights),
        100,
        10**9,
        "fixture",
        BeliefDiagnostics(),
    )


def make_plan(belief, distributions=None):
    distributions = distributions or {
        "a": tuple((WeightedValue(0.8, 1),) for _ in belief.candidates),
        "b": tuple((WeightedValue(0.3, 1),) for _ in belief.candidates),
    }
    calibrated = belief.weight_kind == BeliefWeightKind.CALIBRATED_PROBABILITY
    actions = []
    for action_id, worlds in distributions.items():
        hypotheses = []
        global_atoms = []
        for candidate, atoms in zip(belief.candidates, worlds):
            missing = max(0, 1 - fsum(x.probability for x in atoms))
            mean = fsum(x.value * x.probability for x in atoms)
            lower = fsum(x.lower_bound * x.probability for x in atoms) - missing
            upper = fsum(x.upper_bound * x.probability for x in atoms) + missing
            hypotheses.append(
                HypothesisActionValue(
                    candidate.hypothesis_id,
                    candidate.weight.value if calibrated else None,
                    None if calibrated else candidate.weight.value,
                    mean,
                    lower,
                    upper,
                    atoms,
                    missing,
                )
            )
            if calibrated:
                global_atoms.extend(
                    WeightedValue(
                        x.value,
                        x.probability * candidate.weight.value,
                        x.lower_bound,
                        x.upper_bound,
                    )
                    for x in atoms
                )
        unknown = max(0, 1 - fsum(x.probability for x in global_atoms))
        lower = (
            fsum(
                h.lower_bound * c.weight.value
                for h, c in zip(hypotheses, belief.candidates)
            )
            - belief.residual_unknown_mass
            if calibrated
            else -1
        )
        upper = (
            fsum(
                h.upper_bound * c.weight.value
                for h, c in zip(hypotheses, belief.candidates)
            )
            + belief.residual_unknown_mass
            if calibrated
            else 1
        )
        mean = (
            fsum(
                h.expected_quality * c.weight.value
                for h, c in zip(hypotheses, belief.candidates)
            )
            if calibrated
            else None
        )
        ranking = fsum(h.expected_quality for h in hypotheses) / len(hypotheses)
        actions.append(
            PlanningActionValue(
                action_id,
                mean,
                lower,
                upper,
                tuple(global_atoms),
                unknown,
                tuple(hypotheses),
                ranking,
            )
        )
    return PlanningResult(
        PlanningOutcome.SELECT,
        actions[0].action_id,
        tuple(actions),
        belief.context,
        belief.hypothesis_set_version,
        tuple(distributions),
        "calibrated" if calibrated else "conditional_score",
        belief.residual_unknown_mass,
        1,
        "completed",
        "model",
        "v1",
        "evaluator",
        "v1",
        1,
        "quality.profile.v1",
        "normalized_quality.v1",
        200,
        10**9,
        500,
        PlanningCounters(),
        "bounded",
        3,
    )


def observation_model(belief, *, informative=True, cost=0.1):
    return PlanningObservationModel(
        belief.context,
        belief.hypothesis_set_version,
        tuple(x.hypothesis_id for x in belief.candidates),
        ((1.0, 0.0), (0.0, 1.0)) if informative else ((0.5, 0.5), (0.5, 0.5)),
        "sensor",
        "v1",
        "sensor.calibration",
        cost,
        "evaluator",
        "v1",
    )
