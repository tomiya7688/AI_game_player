"""有限の絶対確率分布を使う、利益の下側CVaRと悲観的な未知tail。"""

from dataclasses import dataclass
from math import fsum, isclose, isfinite


# {
#   責務: [PlanningTailRisk: 未表現質量も含む利益分布の保守的な下振れ指標を保持する]
#   フィールド: [expected_lower/upper: 未知を-1/+1に置いた期待値区間,
#     lower_cvar: 最悪alpha質量の平均利益, lower_quantile: alpha分位利益,
#     worst_value: 最低利益, catastrophic_probability_upper: 閾値以下の利益の最大確率,
#     unknown_mass: 明示的なtrace/探索/Beliefの未表現質量。確率校正は呼出側が確認する]
# }
@dataclass(frozen=True, slots=True)
class PlanningTailRisk:
    expected_lower: float
    expected_upper: float
    lower_cvar: float
    lower_quantile: float
    worst_value: float
    catastrophic_probability_upper: float
    unknown_mass: float


# {
#   責務: [planning_value: normalized planning valueまたは同単位のcostの有限性を検証する]
#   処理: [bool、巨大整数、非有限値と範囲外を拒否する]
#   引数: [value: 対象値, lower/upper: 包含範囲]
#   戻り値: [float: 検証済み値]
#   エラー: [ValueError: 数値契約違反]
# }
def planning_value(value: object, lower: float = -1.0, upper: float = 1.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or (type(value) is int and value.bit_length() > 1024)
        or not isfinite(value)
        or not lower <= value <= upper
    ):
        raise ValueError("planning value violates its finite bounds")
    return float(value)


# {
#   責務: [lower_tail_risk: chanceとBelief双方を含む絶対質量から下側riskを計算する]
#   処理: [欠落質量を再正規化せず-1へ置き、境界atomの一部も含めてCVaRを積分する]
#   引数: [outcomes: 最大4096の(value[-1,1], absolute probability) tuple,
#     unknown_mass: 分布に無い質量, alpha: 最悪側の割合, catastrophic_value: 損失判定の上限]
#   戻り値: [PlanningTailRisk: 未知を悲観したtailと期待値区間]
#   エラー: [ValueError: 不正質量・可変/巨大分布・不正パラメータ]
# }
def lower_tail_risk(
    outcomes: tuple[tuple[float, float], ...],
    unknown_mass: float,
    *,
    alpha: float = 0.05,
    catastrophic_value: float = -0.9,
) -> PlanningTailRisk:
    alpha = planning_value(alpha, 0.0, 1.0)
    if alpha == 0:
        raise ValueError("tail alpha must be positive")
    catastrophic_value = planning_value(catastrophic_value)
    unknown_mass = planning_value(unknown_mass, 0.0, 1.0)
    if type(outcomes) is not tuple or len(outcomes) > 4096:
        raise ValueError("planning distribution exceeds immutable bounds")
    checked = []
    for atom in outcomes:
        if type(atom) is not tuple or len(atom) != 2:
            raise ValueError("planning outcome must contain value and mass")
        value = planning_value(atom[0])
        mass = planning_value(atom[1], 0.0, 1.0)
        if mass:
            checked.append((value, mass))
    known_mass = fsum(mass for _, mass in checked)
    if not isclose(known_mass + unknown_mass, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("planning distribution must preserve total mass")
    # Floating tolerance is charged to unknown, never silently distributed over known atoms.
    unknown_mass = max(unknown_mass, 1.0 - known_mass)
    expected_known = fsum(value * mass for value, mass in checked)
    catastrophe = fsum(mass for value, mass in checked if value <= catastrophic_value)
    if unknown_mass:
        checked.append((-1.0, unknown_mass))
        catastrophe += unknown_mass
    checked.sort()
    remaining = alpha
    average = 0.0
    quantile = checked[-1][0]
    for value, mass in checked:
        take = min(remaining, mass)
        # Divide the probability first so a subnormal alpha cannot erase the loss.
        average += value * (take / alpha)
        remaining -= take
        if remaining <= 0:
            quantile = value
            break
    return PlanningTailRisk(
        max(-1.0, expected_known - unknown_mass),
        min(1.0, expected_known + unknown_mass),
        max(-1.0, min(1.0, average)),
        quantile,
        checked[0][0],
        min(1.0, catastrophe),
        unknown_mass,
    )
