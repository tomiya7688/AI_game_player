from pathlib import Path

from ai_game_player.action_executor import ActionExecutor, ExecutionResult
from ai_game_player.action_safety import (
    ActionSafetyAuditLog,
    ActionSafetyEvaluator,
    ActionSafetyResult,
    SafetyEvaluationContext,
    SafetyStatus,
)
from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.decision_verifier import ReliabilityStatus
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.fail_safe_runtime import FailSafeConfig, FailSafeRuntime, FailSafeState
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.observation_source import ObservationSource
from ai_game_player.ocr_detector import OcrTextCandidateDetector
from ai_game_player.run_control import RunController
from ai_game_player.safety_guard import EmergencyStop, SafetyGuard, SafetyGuardConfig


# {
#   責務: [
#     DecisionPipeline: 観測・Decision・Reliability・Safety・実行を順に接続する
#   ]
#   フィールド: [
#     engine: Decision Context・候補選択・reliability検証
#     safety_evaluator: 実行前のAction Safety検査
#     executor: dry-runまたは明示許可された実行手段
#   ]
#   処理: [
#     1: 観測と候補から判断を得る
#     2: reliability evidenceをAction Safety監査へ渡す
#     3: dry-run設定とverification状態を確認して実行する
#   ]
# }
class DecisionPipeline:
    def __init__(
        self,
        source: ObservationSource,
        game_directory: Path,
        provider: object | None = None,
        controller: RunController | None = None,
        dry_run: bool = True,
        window_handle: int | None = None,
        input_mode: str = "mouse",
        safety_evaluator: ActionSafetyEvaluator | None = None,
        safety_guard: SafetyGuard | None = None,
        safety_guard_config: SafetyGuardConfig | None = None,
        emergency_stop: EmergencyStop | None = None,
        fail_safe_runtime: FailSafeRuntime | None = None,
        fail_safe_config: FailSafeConfig | None = None,
        external_watchdog: bool = True,
    ) -> None:
        self.source = source
        self.ocr = OcrTextCandidateDetector()
        self.merger = CandidateMerger()
        self.engine = GamePlayerEngine(game_directory, provider)
        self.controller = controller or RunController()
        self._seen_rearm_token = 0
        self.executor = ActionExecutor(
            dry_run,
            window_handle=window_handle,
            input_mode=input_mode,
            safety_guard=safety_guard,
            safety_config=safety_guard_config,
            safety_log_path=game_directory / "safety_guard.jsonl",
            emergency_stop=emergency_stop,
            fail_safe_runtime=fail_safe_runtime,
            fail_safe_config=fail_safe_config,
            fail_safe_state_directory=game_directory / "fail_safe",
            external_watchdog=external_watchdog,
        )
        self.execution_history = ExecutionHistory(game_directory / "execution_history.json")
        self.safety_evaluator = safety_evaluator or ActionSafetyEvaluator()
        self.safety_audit = ActionSafetyAuditLog(game_directory / "action_safety.json")
        self.last_safety_result: ActionSafetyResult | None = None

    def _read_candidates(self, ocr_texts: list[dict[str, object]] | None = None) -> tuple[ScreenObservation, list[ActionCandidate]]:
        observation, configured = self.source.read()
        runtime = self.executor.fail_safe_runtime
        if runtime is not None and runtime.state == FailSafeState.ACTIVE:
            self.executor.record_observation()
        detected = self.ocr.detect(observation, ocr_texts if ocr_texts is not None else observation.features.get("ocr_candidates", []))
        image = [ActionCandidate.from_dict(value) for value in observation.features.get("image_candidates", []) if isinstance(value, dict)]
        return observation, self.merger.merge(configured, detected, image)

    def _decide(
        self,
        ocr_texts: list[dict[str, object]] | None = None,
        purpose: str = "",
        personality: str = "",
    ) -> tuple[ActionDecision, list[ActionCandidate], ScreenObservation]:
        observation, candidates = self._read_candidates(ocr_texts)
        decision = self.engine.step(observation, candidates, purpose, personality)
        return decision, candidates, observation

    def run(self, ocr_texts: list[dict[str, object]] | None = None, purpose: str = "", personality: str = "") -> ActionDecision:
        self.controller.ensure_running()
        self._sync_runtime_rearm()
        decision, _, _ = self._decide(ocr_texts, purpose, personality)
        return decision

    # {
    #   責務: [
    #     run_and_execute: 判断を検証・安全評価して安全な場合だけ操作を実行する
    #   ]
    #   処理: [
    #     1: 現在snapshotの候補判断を取得する
    #     2: 選択候補をAction Safety Evaluatorへ渡す
    #     3: reliability・safetyのverification要求を実行前に適用する
    #     4: 実行結果と監査Evidenceを保存する
    #   ]
    #   引数: [
    #     ocr_texts: 任意のOCR候補
    #     purpose: 現在のゲーム目的
    #     personality: Provider向けの任意の振る舞い指定
    #   ]
    #   戻り値: [
    #     ExecutionResult: dry-runまたは実行結果
    #   ]
    #   エラー: [
    #     RuntimeError: decision・safety・reliabilityが実行を拒否
    #   ]
    # }
    def run_and_execute(
        self,
        ocr_texts: list[dict[str, object]] | None = None,
        purpose: str = "",
        personality: str = "",
    ) -> ExecutionResult:
        self.controller.ensure_running()
        self._sync_runtime_rearm()
        decision, candidates, observation = self._decide(ocr_texts, purpose, personality)
        selected = next((candidate for candidate in candidates if candidate.action_id == decision.action_id), None)
        if selected is None:
            self.engine.mark_last_decision_not_executed("決定した候補が統合済み候補にありません")
            raise RuntimeError("決定された候補が統合済み候補にありません")

        reliability_result = self.engine.last_reliability_result
        reliability_record = reliability_result.to_dict() if reliability_result is not None else None
        safety_context, snapshot_id = self._safety_context(selected.action_id, purpose, reliability_record)
        assessment = self.safety_evaluator.evaluate(observation, selected, safety_context)
        self.last_safety_result = assessment
        self.safety_audit.append_evaluation(assessment, snapshot_id=snapshot_id, goal=purpose)
        if assessment.status == SafetyStatus.BLOCK:
            self.engine.mark_last_decision_not_executed(f"Action Safety Evaluator blocked action: {selected.action_id}")
            raise RuntimeError(f"Action Safety Evaluator blocked action: {selected.action_id}")
        if assessment.requires_verification and not self.executor.dry_run:
            requests = ", ".join(assessment.verification_requests)
            self.engine.mark_last_decision_not_executed(f"Action Safety verification required: {requests}")
            raise RuntimeError(f"Action Safety Evaluator requires verification before live input: {requests}")
        if (
            reliability_result is not None
            and reliability_result.status != ReliabilityStatus.TRUST
            and not self.executor.dry_run
        ):
            self.engine.mark_last_decision_not_executed(
                f"Decision Reliability requires verification: {reliability_result.status.value}"
            )
            raise RuntimeError(
                "Decision Reliability requires verification before live input: "
                f"{reliability_result.status.value}"
            )

        result = self.executor.execute(selected)
        self.execution_history.append(result)
        self.safety_audit.append_execution(assessment.assessment_id, result)
        return result

    def record_safety_outcome(self, status: str, confidence: float, evidence: str = "") -> None:
        if self.last_safety_result is None:
            raise RuntimeError("no action safety assessment is available")
        self.safety_audit.append_outcome(self.last_safety_result.assessment_id, status, confidence, evidence)
        normalized = status.casefold()
        changed = normalized in {"ongoing", "success"} or "screen signature changed" in evidence.casefold()
        self.executor.record_progress(changed)

    def trigger_emergency_stop(self, reason: str = "emergency stop requested") -> None:
        self.controller.stop()
        self.executor.trigger_emergency_stop(reason)

    def rearm_safety(self) -> None:
        self.controller.start()
        self._sync_runtime_rearm()

    def close(self) -> None:
        self.executor.close()

    def _sync_runtime_rearm(self) -> None:
        runtime = self.executor.fail_safe_runtime
        if runtime is None:
            return
        token = self.controller.rearm_token
        if token <= self._seen_rearm_token:
            return
        self.executor.rearm_safety()
        self._seen_rearm_token = token

    # {
    #   責務: [
    #     _safety_context: 判断traceから安全評価向けcontextとsnapshot IDを組み立てる
    #   ]
    #   処理: [
    #     1: 直近traceから選択候補のutility evidenceを探す
    #     2: Decision Reliabilityを独立したevidenceとして添える
    #     3: SafetyEvaluationContextとsnapshot IDを返す
    #   ]
    #   引数: [
    #     action_id: 安全評価対象の候補ID
    #     purpose: 現在のゲーム目的
    #     decision_reliability: Decision Verifierの任意監査記録
    #   ]
    #   戻り値: [
    #     tuple[SafetyEvaluationContext, str]: 評価contextとsnapshot ID
    #   ]
    # }
    def _safety_context(
        self,
        action_id: str,
        purpose: str,
        decision_reliability: dict[str, object] | None = None,
    ) -> tuple[SafetyEvaluationContext, str]:
        recent = self.engine.trace.recent(1)
        if not recent:
            return SafetyEvaluationContext(current_goal=purpose, decision_reliability=decision_reliability), ""
        entry = recent[-1]
        raw_context = entry.get("context", {})
        if not isinstance(raw_context, dict):
            return (
                SafetyEvaluationContext(current_goal=purpose, decision_reliability=decision_reliability),
                str(entry.get("snapshot_id", "")),
            )
        utility_score = None
        utility_confidence = None
        candidates = raw_context.get("candidates", [])
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict) or str(candidate.get("action_id", "")) != action_id:
                    continue
                evaluation = candidate.get("evaluation", {})
                if isinstance(evaluation, dict):
                    score = evaluation.get("score")
                    confidence = evaluation.get("confidence")
                    utility_score = float(score) if isinstance(score, (int, float)) else None
                    utility_confidence = float(confidence) if isinstance(confidence, (int, float)) else None
                break
        return (
            SafetyEvaluationContext(
                current_goal=purpose,
                utility_score=utility_score,
                utility_confidence=utility_confidence,
                decision_reliability=decision_reliability,
            ),
            str(raw_context.get("snapshot_id", entry.get("snapshot_id", ""))),
        )
