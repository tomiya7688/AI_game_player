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
#     DecisionPipeline: 画面入力から候補判断・危険度検査・安全な入力実行までの順序を管理する
#   ]
#   フィールド: [
#     engine: 画面候補から判断を選び、画面・許可候補との整合を記録する処理
#     safety_evaluator: 選択操作の対象範囲や不可逆性を、入力送信前に規則で調べる処理
#     executor: 既定では実入力を送らず、設定で許可された場合だけ画面へ操作を送る実行器
#   ]
#   処理: [
#     1: 画面・文字認識・部品検出から統合候補を作り、許可候補から操作を選ぶ
#     2: 選択の信頼性記録を危険度評価へ渡し、二つの判定根拠を別々に保存する
#     3: 実入力では危険判定と追加確認の条件を満たした場合だけ操作を送る。dry-runでは送信せず結果を記録する
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
    #     run_and_execute: 現在画面の判断と安全条件を確かめ、設定に応じて実入力またはdry-runを行う
    #   ]
    #   処理: [
    #     1: 画面を取り込み、候補を統合して現在の画面に結び付いた選択を得る
    #     2: 選択候補を危険度検査へ渡し、実行前確認が必要か判定する
    #     3: BLOCKは常に停止し、実入力では安全性または選択信頼性の追加確認が残る場合も停止する
    #     4: 設定がdry-runなら入力を送らず、それ以外は許可後に実行し、結果と監査記録を保存する
    #   ]
    #   引数: [
    #     ocr_texts: 画面から別途得た文字列候補。省略時は観測に含まれるOCR候補を使う
    #     purpose: 判断元と危険度検査に渡す、今回のゲーム内達成目的
    #     personality: 判断元へ渡す任意の応答方針。候補の許可状態や安全条件を変える設定ではない
    #   ]
    #   戻り値: [
    #     ExecutionResult: 入力未送信のdry-run結果、または実際に送信した操作の実行結果
    #   ]
    #   エラー: [
    #     RuntimeError: 選択候補が統合一覧にない、安全判定がBLOCK、または実入力前の確認条件を満たさない場合
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
    #     _safety_context: 今回の判断履歴から、危険度評価に必要な目的・候補評価・検査記録を取り出す
    #   ]
    #   処理: [
    #     1: 直近判断記録から指定候補の目的寄与点と、その予測の確信度を探す
    #     2: 候補選択の信頼性記録を危険度情報と混ぜずに添える
    #     3: 記録がない場合は未評価項目のまま安全評価用入力を返す
    #   ]
    #   引数: [
    #     action_id: 安全評価する操作候補を、今回の判断履歴から特定するID
    #     purpose: 操作が達成に役立つか照合するための現在のゲーム目的
    #     decision_reliability: 同じ選択に対して先に行った画面・候補整合検査の記録。未取得ならNone
    #   ]
    #   戻り値: [
    #     tuple[SafetyEvaluationContext, str]: 危険度評価に渡す値と、根拠の判断を特定する画面観測ID
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
