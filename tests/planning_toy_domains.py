"""Planning worker用の軽量toy domain fixture。

worker processは登録された``module_name``をcompute budget内でimportする。
unittest/mockやテスト本体を含むmoduleをimportさせると、worker起動の
固定費だけで予算を使い切るため、factoryはこのmoduleへ分離する。
"""

from dataclasses import replace
import json
import time

from ai_game_player.evaluation_quality import NumericQuality
from ai_game_player.planning_contract import PlanningDomain
from ai_game_player.transition_model import ActionTransition, ChanceOutcome


class ToyModel:
    def __init__(self, behavior="table"):
        self.behavior = behavior
        self.calls = 0

    def apply_action(self, state, action_id):
        self.calls += 1
        if self.behavior == "slow" or (self.behavior == "slow_deep" and state >= 10):
            time.sleep(2)
        if self.behavior == "allocate":
            _ = bytearray(512 * 1024 * 1024)
        if self.behavior in ("depth", "transposition"):
            return ActionTransition(
                1 if self.behavior == "transposition" else state + 1, True, 0
            )
        return ActionTransition(
            ({"left": 10, "right": 30}[action_id] + state * 10), True, 0
        )

    def chance_outcomes(self, transition):
        if self.behavior == "split_legal":
            return (ChanceOutcome(10, 0.5), ChanceOutcome(11, 0.5))
        if self.behavior == "invalid_probability":
            return (ChanceOutcome(11, 0.8), ChanceOutcome(12, 0.8))
        if self.behavior == "huge_probability":
            return (ChanceOutcome(11, 10**500),)
        if self.behavior == "wrong_state":
            return (ChanceOutcome("wrong", 1.0),)
        if self.behavior == "oversize":
            return (ChanceOutcome(2**300, 1.0),)
        if self.behavior == "depth":
            return (ChanceOutcome(transition.state, 1),)
        if self.behavior == "transposition":
            return (
                ChanceOutcome(transition.state, 0.4),
                ChanceOutcome(transition.state, 0.6),
            )
        first = (
            0.75 if transition.state == 10 else 0.25 if transition.state == 20 else 0.5
        )
        return (
            ChanceOutcome(transition.state + 1, first),
            ChanceOutcome(transition.state + 2, 1 - first),
        )


class ToyQuality:
    def __init__(self, behavior="table"):
        self.behavior = behavior

    def evaluate(self, state):
        if self.behavior == "slow_quality":
            time.sleep(2)
        if self.behavior == "unknown_quality":
            return NumericQuality()
        value = {
            11: 0.8,
            12: -0.8,
            21: 0.2,
            22: 0.6,
            31: 0.3,
            32: 0.3,
            41: 0.8,
            42: 0.8,
        }.get(state, min(1, state / 10))
        support = 0.5 if self.behavior == "partial_quality" else 1.0
        return NumericQuality(
            value=value, confidence=support, available=True, coverage=support
        )


def decode_int(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"int"}:
        raise ValueError("expected int typed state")
    return int(value["int"])


def domain(behavior):
    def legal(state):
        if behavior == "split_legal" and state in (10, 11):
            return ("left",) if state == 10 else ("right",)
        if behavior in ("depth", "slow_deep"):
            return ("left",)
        if behavior == "intersection" and state == 1:
            return ("right",)
        return ("left", "right") if state < 10 else ()

    return PlanningDomain(
        int, decode_int, ToyModel(behavior), legal, ToyQuality(behavior)
    )


def split_legal_domain():
    return domain("split_legal")


def unexpected_factory_domain():
    raise RuntimeError("private-model-string-must-not-be-recorded")


def table_domain():
    return domain("table")


def depth_domain():
    return domain("depth")


def cache_domain():
    return domain("transposition")


def slow_domain():
    return domain("slow")


def slow_deep_domain():
    return domain("slow_deep")


def slow_quality_domain():
    return domain("slow_quality")


def allocate_domain():
    return domain("allocate")


def invalid_domain():
    return domain("invalid_probability")


def huge_probability_domain():
    return domain("huge_probability")


def wrong_state_domain():
    return domain("wrong_state")


def oversize_domain():
    return domain("oversize")


def intersection_domain():
    return domain("intersection")


def disjoint_legal_domain():
    value = table_domain()
    return replace(
        value, legal_actions=lambda state: ("left",) if state == 0 else ("right",)
    )


def terminal_domain():
    return replace(table_domain(), legal_actions=lambda state: ())


def unknown_quality_domain():
    return domain("unknown_quality")


def partial_quality_domain():
    return domain("partial_quality")


class HiddenWorldModel:
    def apply_action(self, state, action_id):
        if state in (0, 1):
            target = state + 10 if action_id == "probe" else 200
        elif state in (10, 11):
            correct = (state == 10 and action_id == "probe") or (
                state == 11 and action_id == "safe"
            )
            target = 100 if correct else 101
        else:
            raise ValueError("terminal state has no action")
        return ActionTransition(target, True, 0)

    def chance_outcomes(self, transition):
        return (ChanceOutcome(transition.state, 1),)


class HiddenWorldQuality:
    def evaluate(self, state):
        return NumericQuality(
            value={100: 1.0, 101: -1.0, 200: 0.3}.get(state, 0.0),
            confidence=1.0,
            available=True,
        )


def hidden_domain():
    return PlanningDomain(
        int,
        decode_int,
        HiddenWorldModel(),
        lambda state: ("probe", "safe") if state in (0, 1, 10, 11) else (),
        HiddenWorldQuality(),
    )


def observed_domain():
    return replace(hidden_domain(), future_state_observable=True)
