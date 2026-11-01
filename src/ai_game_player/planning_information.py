"""完全情報の上界と、明示観測channelから計算する観測情報価値を分離する。"""

from dataclasses import dataclass
from math import fsum, isclose

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.belief_validation import belief_id, belief_integer
from ai_game_player.planning_risk import planning_value


# {
#   責務: [PlanningObservationModel: 再観測で得るsignalの校正済み条件付き確率を固定する]
#   フィールド: [context/belief_version: 評価対象, hypothesis_ids: likelihood列の順番,
#     signal_likelihoods: signal行×hypothesis列のq(signal|world)。各列の和は1,
#     model_id/model_version/calibration_id: trusted producerと校正の参照,
#     evaluator_id/evaluator_version/quality_profile_version: costとQの同じ価値尺度を照合する参照,
#     cost: normalized planning valueと同じ単位の観測費用。自己申告confidenceではない,
#     withhold_value: 保留した場合の同単位価値。未計測ならNone、便宜的に0としない]
# }
@dataclass(frozen=True, slots=True)
class PlanningObservationModel:
    context: BeliefContext
    belief_version: int
    hypothesis_ids: tuple[str, ...]
    signal_likelihoods: tuple[tuple[float, ...], ...]
    model_id: str
    model_version: str
    calibration_id: str
    cost: float
    evaluator_id: str
    evaluator_version: str
    value_unit: str = "normalized_quality.v1"
    withhold_value: float | None = None
    quality_profile_version: str = "default.1"

    # {
    #   責務: [__post_init__: 有限観測channelの出所・shape・各条件付き分布を検証する]
    #   処理: [最大16signal/256world、可変参照、ID重複、非確率入力を拒否する]
    #   引数: [self: 構築対象]
    #   戻り値: [なし]
    #   エラー: [ValueError: 不正channelまたは出所]
    # }
    def __post_init__(self) -> None:
        if type(self.context) is not BeliefContext:
            raise ValueError("observation model requires a belief context")
        belief_integer(self.belief_version, 1)
        for identifier in (
            self.model_id,
            self.model_version,
            self.calibration_id,
            self.evaluator_id,
            self.evaluator_version,
            self.value_unit,
            self.quality_profile_version,
        ):
            belief_id(identifier)
        planning_value(self.cost, 0.0, 2.0)
        if self.withhold_value is not None:
            planning_value(self.withhold_value)
        if (
            type(self.hypothesis_ids) is not tuple
            or not 1 <= len(self.hypothesis_ids) <= 256
            or len(set(self.hypothesis_ids)) != len(self.hypothesis_ids)
            or type(self.signal_likelihoods) is not tuple
            or not 1 <= len(self.signal_likelihoods) <= 16
        ):
            raise ValueError("observation channel exceeds immutable bounds")
        for identifier in self.hypothesis_ids:
            belief_id(identifier)
        for row in self.signal_likelihoods:
            if type(row) is not tuple or len(row) != len(self.hypothesis_ids):
                raise ValueError("observation channel shape mismatch")
            for value in row:
                planning_value(value, 0.0, 1.0)
        for column in zip(*self.signal_likelihoods):
            if not isclose(fsum(column), 1.0, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("observation channel probability does not equal one")


# {
#   責務: [PlanningInformationValue: 完全情報上界と実観測channelの価値区間を区別する]
#   フィールド: [evpi_upper_bound: 完全情報を得る利益の上界、観測利益ではない,
#     evsi_lower/upper: 入力channelで得る観測価値の区間。Noneはchannelなし,
#     exact: Qが点値かつunknown0の場合だけモデル内EVSIが点値になる]
# }
@dataclass(frozen=True, slots=True)
class PlanningInformationValue:
    evpi_upper_bound: float
    evsi_lower: float | None = None
    evsi_upper: float | None = None
    exact: bool = False


# {
#   責務: [information_value: interval Qと校正済み観測channelから情報価値を有界計算する]
#   処理: [最大256world×64action。未知worldの最大影響を差引き、過大なEVSIを主張しない]
#   引数: [weights: 校正済みworldの絶対質量, action_bounds: world×actionの(lower,upper),
#     unknown_mass: 未表現world質量, channel: 任意の同じworld順のq(signal|world)]
#   戻り値: [PlanningInformationValue: EVPI上界と任意EVSI区間]
#   エラー: [ValueError: shape/分布/値域違反]
# }
def information_value(
    weights: tuple[float, ...],
    action_bounds: tuple[tuple[tuple[float, float], ...], ...],
    unknown_mass: float,
    channel: tuple[tuple[float, ...], ...] | None = None,
) -> PlanningInformationValue:
    unknown_mass = planning_value(unknown_mass, 0.0, 1.0)
    if (
        type(weights) is not tuple
        or not 1 <= len(weights) <= 256
        or type(action_bounds) is not tuple
        or len(action_bounds) != len(weights)
    ):
        raise ValueError("information value requires bounded matching worlds")
    if type(action_bounds[0]) is not tuple:
        raise ValueError("information value matrix requires immutable rows")
    count = len(action_bounds[0])
    if not 1 <= count <= 64:
        raise ValueError("information value action bound exceeded")
    for weight, row in zip(weights, action_bounds):
        planning_value(weight, 0.0, 1.0)
        if type(row) is not tuple or len(row) != count:
            raise ValueError("information value matrix shape mismatch")
        for pair in row:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("information value requires value intervals")
            lower, upper = map(planning_value, pair)
            if lower > upper:
                raise ValueError("information value interval is reversed")
    if not isclose(fsum(weights) + unknown_mass, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("information value world mass does not equal one")
    pre_lower = max(
        fsum(w * row[a][0] for w, row in zip(weights, action_bounds))
        for a in range(count)
    )
    pre_upper = max(
        fsum(w * row[a][1] for w, row in zip(weights, action_bounds))
        for a in range(count)
    )
    perfect_upper = fsum(
        w * max(pair[1] for pair in row) for w, row in zip(weights, action_bounds)
    )
    evpi = max(0.0, min(2.0, perfect_upper - pre_lower + 2 * unknown_mass))
    if channel is None:
        return PlanningInformationValue(evpi)
    if type(channel) is not tuple or not 1 <= len(channel) <= 16:
        raise ValueError("information value channel exceeds bounds")
    for signal in channel:
        if type(signal) is not tuple or len(signal) != len(weights):
            raise ValueError("information value signal shape mismatch")
        for value in signal:
            planning_value(value, 0.0, 1.0)
    if any(
        not isclose(fsum(col), 1.0, rel_tol=0.0, abs_tol=1e-12) for col in zip(*channel)
    ):
        raise ValueError("information value channel is not conditional probability")
    posterior_lower = fsum(
        max(
            fsum(w * q * row[a][0] for w, q, row in zip(weights, signal, action_bounds))
            for a in range(count)
        )
        for signal in channel
    )
    posterior_upper = fsum(
        max(
            fsum(w * q * row[a][1] for w, q, row in zip(weights, signal, action_bounds))
            for a in range(count)
        )
        for signal in channel
    )
    lower = max(0.0, min(evpi, posterior_lower - pre_upper - 2 * unknown_mass))
    upper = max(lower, min(evpi, posterior_upper - pre_lower + 2 * unknown_mass))
    exact = unknown_mass == 0 and all(
        lo == hi for row in action_bounds for lo, hi in row
    )
    return PlanningInformationValue(evpi, lower, upper, exact)
