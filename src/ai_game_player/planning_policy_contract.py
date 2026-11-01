"""Risk/情報価値policyの有限設定と、実行権限を持たない監査結果。"""

from dataclasses import dataclass

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.belief_validation import belief_id, belief_integer
from ai_game_player.planning_information import PlanningInformationValue
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_risk import planning_value


# {
#   責務: [PlanningRiskPolicyConfig: 利益の下振れを許容するsoft objectiveとpolicy処理上限を固定する]
#   フィールド: [tail_alpha: 最悪側の割合, min_cvar: 下側CVaR最低値, catastrophic_value/max_catastrophe: 損失閾値/最大率,
#     risk_aversion: 期待値とCVaRの混合係数, allow_single_conditional: 未校正単一worldの実験を明示許可,
#     max_processing_ns/max_trace_atoms: callbackを使わない数値評価の上限]
# }
@dataclass(frozen=True, slots=True)
class PlanningRiskPolicyConfig:
    tail_alpha: float = 0.05
    min_cvar: float = -0.5
    catastrophic_value: float = -0.9
    max_catastrophe: float = 0.01
    risk_aversion: float = 0.25
    allow_single_conditional: bool = False
    max_processing_ns: int = 100_000_000
    max_trace_atoms: int = 8192

    # {
    #   責務: [__post_init__: 利益尺度と有界処理設定を検証する]
    #   処理: [非有限値、alpha0、無制限budgetを拒否する。Safety設定を持たない]
    #   引数: [self: policy設定]
    #   戻り値: [なし]
    #   エラー: [ValueError: 設定違反]
    # }
    def __post_init__(self) -> None:
        if planning_value(self.tail_alpha, 0, 1) == 0:
            raise ValueError("planning policy tail alpha must be positive")
        planning_value(self.min_cvar)
        planning_value(self.catastrophic_value)
        planning_value(self.max_catastrophe, 0, 1)
        planning_value(self.risk_aversion, 0, 1)
        if type(self.allow_single_conditional) is not bool:
            raise ValueError("conditional planning requires an explicit boolean")
        belief_integer(self.max_processing_ns, 1, 1_000_000_000)
        belief_integer(self.max_trace_atoms, 1, 8192)


# {
#   責務: [PlanningComputationEstimate: 外部の追加計算利益上界と同価値単位費用を受ける]
#   フィールド: [context/belief_version: 同じ計算対象, upper_gain/cost: normalized value単位,
#     source_ref: marginal estimator出所, evaluator_id/evaluator_version: 価値尺度の対応]
# }
@dataclass(frozen=True, slots=True)
class PlanningComputationEstimate:
    context: BeliefContext
    belief_version: int
    upper_gain: float
    cost: float
    source_ref: str
    evaluator_id: str
    evaluator_version: str
    value_unit: str = "normalized_quality.v1"
    quality_profile_version: str = "default.1"

    # {
    #   責務: [__post_init__: 外部見積を有限の出所付き入力へ限定する]
    #   処理: [callbackやmodel自己申告confidenceをgainへ変換しない]
    #   引数: [self: 外部見積]
    #   戻り値: [なし]
    #   エラー: [ValueError: 型/数値/出所違反]
    # }
    def __post_init__(self) -> None:
        if type(self.context) is not BeliefContext:
            raise ValueError("computation estimate requires typed context")
        belief_integer(self.belief_version, 1)
        planning_value(self.upper_gain, 0, 2)
        planning_value(self.cost, 0, 2)
        for value in (
            self.source_ref,
            self.evaluator_id,
            self.evaluator_version,
            self.value_unit,
            self.quality_profile_version,
        ):
            belief_id(value)


# {
#   責務: [PlanningActionRisk: chanceを含む予測の保守的なrisk区間と採否を記録する]
#   フィールド: [action_id: 許可集合内の操作, expected_lower/upper: 利益区間,
#     cvar_lower/upper: 下側CVaR区間, lower_quantile/worst_value: 悲観tail,
#     catastrophic_upper/unknown_mass: 閾値以下質量上界とtrace不保持質量,
#     score: risk混合順位, acceptable: soft risk条件適合。Safety許可ではない,
#     numeric_roundoff_guard: 内部float集約のpadding最大値。統計CIではない,
#     numerical_threshold_indeterminate: 丸め幅内の閾値を有利に確定せず保留した場合True]
# }
@dataclass(frozen=True, slots=True)
class PlanningActionRisk:
    action_id: str
    expected_lower: float
    expected_upper: float
    cvar_lower: float
    cvar_upper: float
    lower_quantile: float
    worst_value: float
    catastrophic_upper: float
    unknown_mass: float
    score: float
    acceptable: bool
    numeric_roundoff_guard: float = 0.0
    numerical_threshold_indeterminate: bool = False


# {
#   責務: [PlanningPolicyDecision: 共通dispositionと観測/計算/未知の監査情報を返す]
#   フィールド: [outcome/selected_action_id: 提案。SELECTでも最終Verifier/Safetyが必要,
#     context/belief_version: 予測元, reason_codes: 固定理由, risks: 許可操作のrisk,
#     disagreement: agreement/disagreement/unknown, information: risk-neutral EVPI上界/任意EVSI,
#     observation_policy_upper_bound: 実際の現在提案を基準にした完全情報の改善上界,
#     observation_policy_gain_lower/upper: 同じrisk制約を適用した再計画の実現可能な期待改善区間,
#     observation_contingent_risk: signal×world×選択action×chanceを合成して再検査した全体risk,
#     probability_semantics: global_calibrated/conditional_hypothesis/unknown,
#     global_risk_known: 未校正worldならFalse, continue_computation: 有益な追加計算の提案,
#     observation_requests/observation_cost/spent_ns: episode消費と今回policy所要,
#     provenance: 評価器/モデル/観測channel/外部gain参照。実観測記録ではない,
#     continuation_semantics: one_step/open_loop_mpc/state_observed_expectimax。無効sourceではunknown,
#     information_scope: root_action_menuまたはfixed_continuation_policy_menu。任意再最適化のVOIではない]
# }
@dataclass(frozen=True, slots=True)
class PlanningPolicyDecision:
    outcome: PlanningOutcome
    selected_action_id: str | None
    context: BeliefContext
    belief_version: int
    reason_codes: tuple[str, ...]
    risks: tuple[PlanningActionRisk, ...] = ()
    disagreement: str = "unknown"
    information: PlanningInformationValue | None = None
    observation_policy_upper_bound: float | None = None
    observation_policy_gain_lower: float | None = None
    observation_policy_gain_upper: float | None = None
    observation_contingent_risk: PlanningActionRisk | None = None
    probability_semantics: str = "unknown"
    global_risk_known: bool = False
    continue_computation: bool = False
    observation_requests: int = 0
    observation_cost: float = 0.0
    spent_ns: int = 0
    provenance: tuple[str, ...] = ()
    continuation_semantics: str = "unknown"
    information_scope: str = "unknown"
