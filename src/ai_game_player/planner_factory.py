"""trusted hostが明示的に選ぶreference計画profile。既定Providerは変更しない。"""

from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.planner_2048_observation import Reference2048PlanningObservation
from ai_game_player.planner_provider import PlannerDecisionProvider
from ai_game_player.planning_contract import SearchBudget
from ai_game_player.planning_policy_contract import PlanningRiskPolicyConfig
from ai_game_player.run_control import RunController


# {
#   責務: [reference_2048_planner_provider: 純粋2048登録と実画面sensorを任意profileで組み立てる]
#   処理: [generic coreにゲーム条件を追加せず、未校正条件付き選択は両budget/policyで明示opt-inする]
#   引数: [controller: 停止照合共有, allow_conditional: 実験単一world選択,
#     search_budget: 有限探索設定。Noneはdepth2/node10000/400ms,
#     ttl_ns: 実験仮説TTL。物理Safety leaseと独立]
#   戻り値: [PlannerDecisionProvider: Safety/Input権限を持たない任意Provider]
# }
def reference_2048_planner_provider(
    *,
    controller: RunController | None = None,
    allow_conditional: bool = False,
    search_budget: SearchBudget | None = None,
    ttl_ns: int = 4_000_000_000,
) -> PlannerDecisionProvider:
    from ai_game_player.bounded_planner import BoundedExpectimaxPlanner
    from ai_game_player.planning_2048 import reference_2048_registration

    clock_id = "python.monotonic"
    budget = search_budget or SearchBudget(
        max_depth=2,
        max_nodes=10000,
        wall_time_ms=400,
        allow_conditional_selection=allow_conditional,
    )
    if budget.allow_conditional_selection != allow_conditional:
        raise ValueError("conditional experiment requires matching budget and policy")
    planner = BoundedExpectimaxPlanner(
        (reference_2048_registration(),), clock_id=clock_id, controller=controller
    )
    return PlannerDecisionProvider(
        planner,
        Reference2048PlanningObservation(clock_id=clock_id, ttl_ns=ttl_ns),
        enabled=True,
        fallback=BaselineDecisionProvider(),
        search_budget=budget,
        risk_config=PlanningRiskPolicyConfig(
            allow_single_conditional=allow_conditional
        ),
    )
