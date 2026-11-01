"""純粋探索とrisk提案を既存のDecision/Verifierへ接続する任意Provider。"""

from collections.abc import Callable
from dataclasses import asdict
import time
from typing import Any, NoReturn

from ai_game_player.baseline_provider import BaselineDecisionProvider
from ai_game_player.belief_likelihood import BeliefActionRef
from ai_game_player.decision_context import DecisionContext
from ai_game_player.decision_withheld import DecisionDisposition, DecisionWithheld
from ai_game_player.execution_receipt import ExecutionReceipt
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.observation_freshness import ObservationFreshness
from ai_game_player.planner_observation import (
    PlannerObservationAdapter,
    PlannerObservationInput,
)
from ai_game_player.planning_contract import SearchBudget
from ai_game_player.planning_wire import result_to_dict
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_policy_contract import (
    PlanningRiskPolicyConfig,
    PlanningPolicyDecision,
)
from ai_game_player.planning_result import PlanningResult
from ai_game_player.planning_reobservation import PlanningDecisionEpisode
from ai_game_player.planning_risk_policy import PlanningRiskPolicy
from ai_game_player.planning_selection_guard import PlanningSelectionGuard


# {
#   責務: [PlannerDecisionProvider: 最初の一手または非SELECTを既存判断境界へ渡す]
#   フィールド: [planner/adapter: 純粋探索とtrusted実観測変換, fallback/enabled: OFF/能力無しのBaseline,
#     _episode/_policy: Frameを越えて維持するVOI予算, _guard: 一手の入力前照合,
#     _action_epoch/_last_actual_capture: 実操作または送信不明の後に新観測を要求する境界]
# }
class PlannerDecisionProvider:
    # {
    #   責務: [__init__: 任意Plannerと既存Baseline fallbackを構成する]
    #   処理: [既定OFF。policyの保留・失効をfallbackで無視せず、予測Outcomeの意味評価APIを公開しない]
    #   引数: [planner: 565契約の純粋探索, adapter: trusted sensor,
    #     enabled: 明示ON, fallback: 能力無し/OFF時の参照判断, search_budget/risk_config: 有限方針,
    #     clock_ns: 同process時計] 戻り値: [なし]
    # }
    def __init__(
        self,
        planner: Any,
        adapter: PlannerObservationAdapter | None,
        *,
        enabled: bool = False,
        fallback: BaselineDecisionProvider | None = None,
        search_budget: SearchBudget | None = None,
        risk_config: PlanningRiskPolicyConfig | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("planning enablement requires an explicit boolean")
        self.planner, self.adapter, self.enabled = planner, adapter, enabled
        self.fallback = fallback or BaselineDecisionProvider()
        self.search_budget = search_budget or SearchBudget()
        self.risk_config = risk_config or PlanningRiskPolicyConfig()
        self.clock_ns = clock_ns
        self._prepared: PlannerObservationInput | None = None
        self._registration: Any = None
        self._context: DecisionContext | None = None
        self._allowed_candidates: tuple[ActionCandidate, ...] = ()
        self._observation: ScreenObservation | None = None
        self._run_generation = 0
        self._action_epoch = 0
        self._episode: PlanningDecisionEpisode | None = None
        self._policy: PlanningRiskPolicy | None = None
        self._guard: PlanningSelectionGuard | None = None
        self._authorized_capture: ObservationFreshness | None = None
        self._last_actual_capture: ObservationFreshness | None = None
        self.last_planning_result: PlanningResult | None = None
        self.last_policy_decision: PlanningPolicyDecision | None = None
        self.last_planning_audit: dict[str, Any] = {}

    # {
    #   責務: [prepare_planning_context: Engineが固定した実観測をこの判断へ結び付ける]
    #   処理: [操作後cachedFrameを拒否し、OFF/能力無しだけをfallback可能として記録する]
    #   引数: [context/observation: 同じ判断材料と実画面, run_generation: RunController世代,
    #     candidates: Engineが評価済みとして固定した元の候補実体]
    #   戻り値: [なし] エラー: [DecisionWithheld: 実観測の鮮度/参照不足]
    # }
    def prepare_planning_context(
        self,
        context: DecisionContext,
        observation: ScreenObservation,
        run_generation: int | None,
        candidates: tuple[ActionCandidate, ...],
    ) -> None:
        self._guard = None
        self._authorized_capture = None
        self._prepared = None
        self._allowed_candidates = candidates
        self._context, self._observation = context, observation
        self._run_generation = 0 if run_generation is None else run_generation
        self.last_planning_audit = {
            "schema": "planning.runtime/v1",
            "status": "disabled" if not self.enabled else "unavailable",
        }
        if not self.enabled or self.planner is None or self.adapter is None:
            return
        try:
            capture = ObservationFreshness.from_observation(observation)
        except ValueError:
            self._withhold("planning_capture_reference_unavailable")
        if self._last_actual_capture is not None and not capture.is_newer_than(
            self._last_actual_capture
        ):
            self._withhold(
                "post_action_observation_required", PlanningOutcome.OBSERVE_MORE
            )
        try:
            prepared = self.adapter.prepare(
                context,
                observation,
                self._action_epoch,
                self._run_generation,
                self.clock_ns(),
            )
        except DecisionWithheld:
            raise
        except (ValueError, TypeError):
            self._withhold("planning_observation_rejected")
        if prepared is None:
            return
        source = prepared.belief.context
        if (
            source.snapshot_id != context.snapshot_id
            or capture.frame_id not in source.frame_ids
            or source.source_id != capture.source_id
            or source.source_generation != capture.source_generation
            or source.action_epoch != self._action_epoch
        ):
            self._withhold("planning_observation_binding_mismatch")
        self._prepared = prepared
        self._registration = getattr(self.planner, "registrations", {}).get(
            prepared.registration_id
        )
        self._assert_current_belief()

    # {
    #   責務: [validate_execution_observation: 操作後Outcomeへcached入力前Frameを渡さない]
    #   処理: [明示完了APIでも同じSourceの新しい実Frameを要求する]
    #   引数: [observation: post-action実観測] 戻り値: [なし]
    #   エラー: [DecisionWithheld: 新観測を証明できない]
    # }
    def validate_execution_observation(self, observation: ScreenObservation) -> None:
        if self._last_actual_capture is None:
            return
        try:
            capture = ObservationFreshness.from_observation(observation)
        except ValueError:
            self._withhold("post_action_reference_unavailable")
        if not capture.is_newer_than(self._last_actual_capture):
            self._withhold(
                "post_action_observation_required", PlanningOutcome.OBSERVE_MORE
            )

    # {
    #   責務: [choose_context: typed SELECTだけをActionDecisionへ変換する]
    #   処理: [全hypothesis探索後にrisk/VOI policyを適用し、予測をactual Outcomeへ渡さない]
    #   引数: [context: 元判断材料, personality: fallback用方針] 戻り値: [ActionDecision: 最初の一手だけ]
    #   エラー: [DecisionWithheld: OBSERVE_MORE/ABSTAIN/ESCALATEまたは失効]
    # }
    def choose_context(
        self, context: DecisionContext, personality: str = ""
    ) -> ActionDecision:
        if self._prepared is None:
            return self.fallback.choose_context(context, personality)
        prepared = self._prepared
        source, now = prepared.belief.context, self.clock_ns()
        if context is not self._context:
            self._withhold("planning_context_changed")
        if now >= prepared.belief.expires_at_ns:
            self._withhold("planning_observation_expired")
        if self._episode is None:
            self._episode = PlanningDecisionEpisode(source, prepared.goal_id, now)
            self._policy = PlanningRiskPolicy(
                self._episode, config=self.risk_config, clock_ns=self.clock_ns
            )
        elif (
            source.run_scope != self._episode.context.run_scope
            or source.action_epoch != self._episode.context.action_epoch
        ):
            self._episode.reset_scope(source, prepared.goal_id, now)
        allowed = tuple(context.allowed_action_ids)
        result = self.planner.plan(
            prepared.belief,
            allowed,
            registration_id=prepared.registration_id,
            budget=self.search_budget,
            current_belief=self._current_belief,
        )
        self._assert_current_belief()
        self.last_planning_result = result
        assert self._policy is not None
        decision = self._policy.decide(
            result,
            prepared.belief,
            context=source,
            goal_id=prepared.goal_id,
            allowed_action_ids=allowed,
            run_generation=self._run_generation,
            deadline_ns=prepared.belief.expires_at_ns,
            observation_model=prepared.observation_model,
        )
        self.last_policy_decision = decision
        self.last_planning_audit = {
            "schema": "planning.runtime/v1",
            "status": decision.outcome.value,
            "prediction": result_to_dict(result),
            "policy": asdict(decision),
            "actual_outcome": False,
        }
        if decision.outcome != PlanningOutcome.SELECT:
            raise DecisionWithheld(
                DecisionDisposition(
                    decision.outcome,
                    decision.reason_codes or ("planning_withheld",),
                    "belief_planner",
                    context.snapshot_id,
                )
            )
        matching = [
            candidate
            for candidate in self._allowed_candidates
            if candidate.action_id == decision.selected_action_id
        ]
        if len(matching) != 1 or matching[0].action_id not in allowed:
            self._withhold("planning_selection_not_allowed")
        candidate = matching[0]
        assert self._observation is not None
        self._guard = PlanningSelectionGuard(
            source,
            prepared.belief.hypothesis_set_version,
            self._run_generation,
            min(result.expires_at_ns, prepared.belief.expires_at_ns),
            prepared.semantic_digest,
            candidate,
            ObservationFreshness.from_observation(self._observation),
            self._observation.screen_id,
            str(self._observation.features.get("signature", "")),
        )
        return ActionDecision(
            candidate.action_id,
            "select the first verified planning action",
            "belief_planner",
            context.snapshot_id,
            context.state["screen_id"],
            context.state["signature"],
        )

    # {
    #   責務: [validate_planned_selection: 入力前に元候補実体と新Frameを再照合する]
    #   処理: [既存Safetyを迂回せず、この一手だけをauthorizeする]
    #   引数: [observation/candidates: 新取得画面と評価済み候補, selected: Engineの元候補実体, generation: 現Run世代]
    #   戻り値: [なし] エラー: [DecisionWithheld: 不一致]
    # }
    def validate_planned_selection(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        selected: ActionCandidate,
        generation: int,
    ) -> None:
        if self._guard is None:
            return
        if self._guard.candidate != selected:
            self._withhold("planning_original_candidate_changed")
        self._assert_current_belief()
        assert self.adapter is not None
        self._authorized_capture = self._guard.validate_fresh(
            observation, candidates, self.adapter, generation, self.clock_ns()
        )

    # {
    #   責務: [check_planned_dispatch: backend直前にも選択失効/Run世代を再検査する]
    #   処理: [sensor IOをせずmemoryと時計だけを照合する]
    #   引数: [generation: 現Run世代] 戻り値: [なし] エラー: [DecisionWithheld: 未照合/失効]
    # }
    def check_planned_dispatch(self, generation: int) -> None:
        if self._guard is not None:
            self._assert_current_belief()
            if self._authorized_capture is None:
                self._withhold("planning_fresh_check_missing")
            self._guard.check_authority(generation, self.clock_ns())

    # {
    #   責務: [_current_belief: 現Storeと登録版を探索中にも再照会する]
    #   処理: [自分のcached入力を最新版と偽らず、登録差替/Scope失効をNoneにする]
    #   引数: [なし] 戻り値: [現行SnapshotまたはNone]
    # }
    def _current_belief(self) -> Any:
        if self.adapter is None or self._prepared is None:
            return None
        if (
            getattr(self.planner, "registrations", {}).get(
                self._prepared.registration_id
            )
            != self._registration
        ):
            return None
        if (
            getattr(self.planner, "clock_id", self._prepared.belief.context.clock_id)
            != self._prepared.belief.context.clock_id
        ):
            return None
        try:
            return self.adapter.current_belief(self._prepared.belief.context)
        except (ValueError, TypeError):
            return None

    # {
    #   責務: [_assert_current_belief: 同画素でも仮説/unknown/校正版の更新を拒否する]
    #   処理: [元版全体との一致を確認し、旧版SELECTを実行しない]
    #   引数: [なし] 戻り値: [なし] エラー: [DecisionWithheld: 旧版/失効/登録差替]
    # }
    def _assert_current_belief(self) -> None:
        if self._prepared is None or self._current_belief() != self._prepared.belief:
            self._withhold("planning_belief_or_registration_changed")

    # {
    #   責務: [acknowledge_planned_execution: 実送信receiptだけで次の実観測を要求する]
    #   処理: [正常実行はepisodeへ一回ack、送信不明は成功化せず再観測境界だけ進める]
    #   引数: [receipt: 現世代で記録済みの実行事実] 戻り値: [なし]
    # }
    def acknowledge_planned_execution(self, receipt: ExecutionReceipt) -> None:
        guard = self._guard
        if guard is None or receipt.execution_status not in {
            "executed",
            "dispatch_unknown",
        }:
            return
        if (
            receipt.snapshot_id != guard.context.snapshot_id
            or receipt.action_id != guard.candidate.action_id
            or receipt.action_generation != guard.run_generation
        ):
            raise RuntimeError("planning execution receipt binding mismatch")
        if receipt.execution_status == "executed":
            assert self._episode is not None
            action = BeliefActionRef(
                receipt.action_id,
                "receipt." + receipt.snapshot_id,
                receipt.snapshot_id,
                guard.context.action_epoch,
                guard.context,
            )
            if not self._episode.acknowledge_execution(action, self.clock_ns()):
                raise RuntimeError("planning execution acknowledgement rejected")
        self._last_actual_capture = self._authorized_capture or guard.capture
        self._action_epoch += 1
        self._guard = None
        self._authorized_capture = None

    # {
    #   責務: [requires_fresh_selection: SELECTの入力前captureが必要か示す]
    #   処理: [OFF/fallbackへPlanner専用条件を誤適用しない] 引数: [なし] 戻り値: [bool]
    # }
    @property
    def requires_fresh_selection(self) -> bool:
        return self._guard is not None

    # {
    #   責務: [_withhold: 無入力の構造化結果で判断境界を脱出する]
    #   処理: [fallbackで保留やexpiryを無視しない] 引数: [reason/outcome: 固定codeと非SELECT]
    #   戻り値: [なし] エラー: [DecisionWithheld: 常に送出]
    # }
    def _withhold(
        self, reason: str, outcome: PlanningOutcome = PlanningOutcome.ABSTAIN
    ) -> NoReturn:
        raise DecisionWithheld(
            DecisionDisposition(
                outcome,
                (reason,),
                "belief_planner",
                self._context.snapshot_id if self._context is not None else None,
            )
        )
