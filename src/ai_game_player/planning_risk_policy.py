"""仮説間の不一致・実観測価値・下振れから、実入力しない提案を選ぶ。"""

from collections.abc import Callable
from dataclasses import replace
from math import fsum
import time
from typing import Any

from ai_game_player.belief_contract import BeliefContext
from ai_game_player.belief_snapshot import BeliefSnapshot
from ai_game_player.belief_validation import belief_id, belief_integer
from ai_game_player.planning_information import (
    PlanningObservationModel,
    information_value,
)
from ai_game_player.planning_outcome import PlanningOutcome
from ai_game_player.planning_observation_policy import observation_policy_gain
from ai_game_player.planning_policy_contract import (
    PlanningComputationEstimate,
    PlanningPolicyDecision,
    PlanningRiskPolicyConfig,
)
from ai_game_player.planning_policy_authority import (
    planning_allowed_actions,
    validate_planning_source,
    validate_planning_estimate,
)
from ai_game_player.planning_policy_projection import (
    project_action_risk,
    project_world_values,
)
from ai_game_player.planning_reobservation import PlanningDecisionEpisode
from ai_game_player.planning_result import PlanningResult
from ai_game_player.planning_risk import planning_value


# {
#   責務: [PlanningRiskPolicy: 許可/鮮度のhard境界を維持し、soft riskとVOIで次の提案を選ぶ]
#   フィールド: [episode: 新Frameでも維持するGoal所有予算, config: soft risk/有限処理設定,
#     _clock: context.clock_idと同じmonotonic origin。重い計算・IOをしないtrusted時計]
# }
class PlanningRiskPolicy:
    # {
    #   責務: [__init__: episode所有と有限policyを接続する]
    #   処理: [入力/worker/Remoteを開始せず、純粋な数値表だけを処理する]
    #   引数: [episode: Goal毎に維持する所有者, config: risk条件, clock_ns: 同clock origin時計]
    #   戻り値: [なし]
    # }
    def __init__(
        self,
        episode: PlanningDecisionEpisode,
        *,
        config: PlanningRiskPolicyConfig | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if type(episode) is not PlanningDecisionEpisode or not callable(clock_ns):
            raise ValueError("planning risk policy requires episode and clock")
        self.episode = episode
        self.config = config or PlanningRiskPolicyConfig()
        if type(self.config) is not PlanningRiskPolicyConfig:
            raise ValueError("planning risk policy requires typed settings")
        self._clock = clock_ns

    # {
    #   責務: [decide: 同じ観測に束縛した探索値を共通Outcomeへ変換する]
    #   処理: [hard allowed/vetoと鮮度を先に確認し、chance tailを評価。実EVSI下限が費用を超える時だけ有界再観測する]
    #   引数: [result/belief: 565探索と元集合, context/goal_id/run_generation: 現在の実行authority,
    #     allowed_action_ids/hard_veto_action_ids: 現在のhard候補境界, deadline_ns: このpolicy処理期限,
    #     observation_model/observation_cost: 校正channelまたは未導入時の同価値単位費用,
    #     computation: 同尺度の追加計算利益上界/費用, can_escalate: 追加判断要求を許可。Remote同意ではない]
    #   戻り値: [PlanningPolicyDecision: SELECTでも最終Verifier/Safetyが必要。保留を入力fallbackで無視しない]
    #   エラー: [ValueError: 現在authority自体が不正。探索の失敗/不正/timeoutはABSTAIN]
    # }
    def decide(
        self,
        result: PlanningResult,
        belief: BeliefSnapshot[Any],
        *,
        context: BeliefContext,
        goal_id: str,
        allowed_action_ids: tuple[str, ...],
        run_generation: int,
        deadline_ns: int,
        hard_veto_action_ids: tuple[str, ...] = (),
        observation_model: PlanningObservationModel | None = None,
        observation_cost: float = 0.1,
        computation: PlanningComputationEstimate | None = None,
        can_escalate: bool = False,
    ) -> PlanningPolicyDecision:
        if type(context) is not BeliefContext or not isinstance(belief, BeliefSnapshot):
            raise ValueError("planning policy requires typed current authority")
        belief_id(goal_id)
        belief_integer(run_generation)
        belief_integer(deadline_ns)
        if type(can_escalate) is not bool:
            raise ValueError("escalation permission must be explicit")
        used_requests, used_cost = self.episode.usage
        decision = PlanningPolicyDecision(
            PlanningOutcome.ABSTAIN,
            None,
            context,
            belief.hypothesis_set_version,
            ("invalid_planning_evidence",),
            observation_requests=used_requests,
            observation_cost=used_cost,
        )
        try:
            started = self._now()
        except Exception:
            return replace(decision, reason_codes=("clock_failure",))
        cutoff = min(deadline_ns, started + self.config.max_processing_ns)
        try:
            self._check_deadline(started, cutoff)
            allowed = planning_allowed_actions(allowed_action_ids, hard_veto_action_ids)
            if not allowed:
                return replace(decision, reason_codes=("no_hard_allowed_action",))
            if not self.episode.check_context(context, goal_id, started):
                return replace(decision, reason_codes=("episode_context_mismatch",))
            validate_planning_source(result, belief, context, run_generation, started)
            cutoff = min(cutoff, result.expires_at_ns, belief.expires_at_ns)
            actions = tuple(
                item for item in result.action_values if item.action_id in allowed
            )
            if (
                not actions
                or result.completed_depth == 0
                or result.outcome != PlanningOutcome.SELECT
            ):
                return replace(decision, reason_codes=("planning_unavailable",))
            continue_computation = False
            computation_reason = None
            if computation is not None:
                validate_planning_estimate(computation, result)
                continue_computation = computation.upper_gain > computation.cost
                computation_reason = (
                    "computation_gain_above_cost"
                    if continue_computation
                    else "computation_early_exit"
                )
            provenance: tuple[str, ...] = (
                result.model_id,
                result.model_version,
                result.evaluator_id,
                result.evaluator_version,
                result.quality_profile_version,
            )
            if computation is not None:
                provenance += (computation.source_ref,)
            decision = replace(decision, provenance=provenance)
            calibrated = result.probability_semantics == "calibrated"
            conditional = (
                not calibrated
                and self.config.allow_single_conditional
                and result.probability_semantics == "conditional_score"
                and len(belief.candidates) == 1
            )
            if not calibrated and not conditional:
                reasons = ["uncalibrated_global_risk"]
                if computation_reason is not None:
                    reasons.append(computation_reason)
                outcome = (
                    PlanningOutcome.ESCALATE
                    if can_escalate and (computation is None or continue_computation)
                    else PlanningOutcome.ABSTAIN
                )
                return self._finish(
                    replace(decision, continue_computation=continue_computation),
                    outcome,
                    reasons,
                    started,
                    cutoff,
                )
            atoms = sum(
                len(item.hypothesis_values[0].conditional_outcomes)
                if conditional and item.hypothesis_values
                else len(item.outcomes)
                for item in actions
            )
            if atoms > self.config.max_trace_atoms:
                return replace(decision, reason_codes=("policy_trace_budget",))
            risks = []
            for action in actions:
                self._check_deadline(started, cutoff)
                if (
                    calibrated
                    and action.trace_unknown_mass + 1e-10 < belief.residual_unknown_mass
                ):
                    raise ValueError("planning trace discards unknown world mass")
                risks.append(
                    project_action_risk(action, self.config, conditional=conditional)
                )
            ids, weights, bounds, disagreement = project_world_values(
                belief, actions, conditional=conditional
            )
            residual = 0.0 if conditional else belief.residual_unknown_mass
            channel = None
            cost = planning_value(observation_cost, 0, 2)
            if observation_model is not None:
                if conditional:
                    raise ValueError("uncalibrated world does not support global EVSI")
                validate_planning_estimate(observation_model, result)
                if observation_model.hypothesis_ids != ids:
                    raise ValueError("observation channel world identity mismatch")
                channel = observation_model.signal_likelihoods
                cost = observation_model.cost
                provenance += (
                    observation_model.model_id,
                    observation_model.model_version,
                    observation_model.calibration_id,
                )
            information = information_value(weights, bounds, residual, channel)
            feasible = [risk for risk in risks if risk.acceptable]
            if feasible:
                baseline_lower = min(
                    feasible, key=lambda item: (-item.score, item.action_id)
                ).expected_lower
            else:
                baseline_lower = (
                    observation_model.withhold_value
                    if observation_model is not None
                    and observation_model.withhold_value is not None
                    else -1.0
                )
            perfect_upper = (
                fsum(
                    weight * max(pair[1] for pair in row)
                    for weight, row in zip(weights, bounds)
                )
                + residual
            )
            policy_information_upper = max(
                0.0, min(2.0, perfect_upper - baseline_lower)
            )
            reasons = [computation_reason] if computation_reason is not None else []
            decision = replace(
                decision,
                risks=tuple(risks),
                disagreement=disagreement,
                information=information,
                observation_policy_upper_bound=policy_information_upper,
                probability_semantics="conditional_hypothesis"
                if conditional
                else "global_calibrated",
                global_risk_known=calibrated,
                continue_computation=continue_computation,
                provenance=provenance,
            )
            self._check_deadline(started, cutoff)
            if policy_information_upper <= cost:
                reasons.append("observation_upper_bound_below_cost")
            elif observation_model is not None:
                gain = (
                    observation_policy_gain(
                        belief,
                        actions,
                        observation_model,
                        tuple(risks),
                        self.config,
                        lambda: self._check_deadline(started, cutoff),
                    )
                    if observation_model is not None
                    else None
                )
                if gain is not None:
                    decision = replace(
                        decision,
                        observation_policy_gain_lower=gain.lower,
                        observation_policy_gain_upper=gain.upper,
                        observation_contingent_risk=gain.contingent_risk,
                    )
                if (
                    gain is None
                    or gain.lower <= cost
                    or not gain.contingent_risk.acceptable
                ):
                    reasons.append("risk_policy_observation_gain_unestablished")
                if gain is not None and not gain.contingent_risk.acceptable:
                    reasons.append("observation_contingent_risk_limit")
                now = self._check_deadline(started, cutoff)
                if (
                    gain is not None
                    and gain.lower > cost
                    and gain.contingent_risk.acceptable
                    and self.episode.request_observation(context, cost, now)
                ):
                    return self._finish(
                        decision,
                        PlanningOutcome.OBSERVE_MORE,
                        reasons + ["positive_net_risk_policy_information"],
                        started,
                        cutoff,
                    )
                if (
                    gain is not None
                    and gain.lower > cost
                    and gain.contingent_risk.acceptable
                ):
                    reasons.append("observation_episode_exhausted")
            else:
                reasons.append("evsi_model_unavailable")
            self._check_deadline(started, cutoff)
            if feasible:
                selected = min(feasible, key=lambda item: (-item.score, item.action_id))
                reasons.append(
                    "conditional_assumption_explicit"
                    if conditional
                    else "risk_acceptable"
                )
                proposed = self._finish(
                    replace(decision, selected_action_id=selected.action_id),
                    PlanningOutcome.SELECT,
                    reasons,
                    started,
                    cutoff,
                )
                if not self.episode.record_selection(context, selected.action_id):
                    raise ValueError("planning episode changed during evaluation")
                return proposed
            reasons.append("risk_limit_exceeded")
            outcome = (
                PlanningOutcome.ESCALATE
                if can_escalate and (computation is None or continue_computation)
                else PlanningOutcome.ABSTAIN
            )
            return self._finish(decision, outcome, reasons, started, cutoff)
        except TimeoutError:
            used_requests, used_cost = self.episode.usage
            return replace(
                decision,
                outcome=PlanningOutcome.ABSTAIN,
                selected_action_id=None,
                reason_codes=("policy_timeout",),
                observation_requests=used_requests,
                observation_cost=used_cost,
                continue_computation=False,
            )
        except (ValueError, TypeError, AttributeError, OverflowError):
            used_requests, used_cost = self.episode.usage
            return replace(
                decision,
                outcome=PlanningOutcome.ABSTAIN,
                selected_action_id=None,
                reason_codes=("invalid_planning_evidence",),
                observation_requests=used_requests,
                observation_cost=used_cost,
                continue_computation=False,
            )

    # {
    #   責務: [_now: 同clock originの非負整数を取得する]
    #   処理: [boolや不正な時計値を拒否する]
    #   引数: [self: 接続時計]
    #   戻り値: [int: monotonic ns]
    #   エラー: [ValueError: 不正な時計]
    # }
    def _now(self) -> int:
        try:
            return belief_integer(self._clock())
        except Exception as error:
            raise ValueError("planning clock is unavailable") from error

    # {
    #   責務: [_check_deadline: 処理境界で時間切れと時計後退を拒否する]
    #   処理: [強制preemptionではなく、期限後の提案公開を止める]
    #   引数: [started/cutoff: 同originの開始/包含しない期限]
    #   戻り値: [int: 検査した現在時刻]
    #   エラー: [TimeoutError: 時計後退または期限]
    # }
    def _check_deadline(self, started: int, cutoff: int) -> int:
        now = self._now()
        if not started <= now < cutoff:
            raise TimeoutError("planning policy deadline exceeded")
        return now

    # {
    #   責務: [_finish: 期限直前にも検査し、消費/所要時間を提案へ固定する]
    #   処理: [episode使用量をコピーし、SELECT以外へ操作IDを残さない]
    #   引数: [decision: 監査材料, outcome/reasons: 結論, started/cutoff: policy時間境界]
    #   戻り値: [PlanningPolicyDecision: 固定した提案]
    #   エラー: [TimeoutError: 公開期限]
    # }
    def _finish(
        self,
        decision: PlanningPolicyDecision,
        outcome: PlanningOutcome,
        reasons: list[str],
        started: int,
        cutoff: int,
    ) -> PlanningPolicyDecision:
        now = self._check_deadline(started, cutoff)
        requests, cost = self.episode.usage
        return replace(
            decision,
            outcome=outcome,
            selected_action_id=decision.selected_action_id
            if outcome == PlanningOutcome.SELECT
            else None,
            reason_codes=tuple(reasons),
            observation_requests=requests,
            observation_cost=cost,
            spent_ns=now - started,
        )
