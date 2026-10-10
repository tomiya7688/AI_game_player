from pathlib import Path
from typing import Callable

from ai_game_player.action_executor import ActionExecutor, ExecutionResult
from ai_game_player.action_safety import (
    ActionSafetyAuditLog,
    ActionSafetyEvaluator,
    ActionSafetyResult,
    SafetyEvaluationContext,
    SafetyStatus,
)
from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.event_journal import EventJournal
from ai_game_player.fail_safe_runtime import FailSafeConfig, FailSafeRuntime, FailSafeState
from ai_game_player.journal_adapters import LegacyEventAdapter
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.observation_source import ObservationSource
from ai_game_player.ocr_detector import OcrTextCandidateDetector
from ai_game_player.run_control import RunController
from ai_game_player.safety_guard import EmergencyStop, SafetyGuard, SafetyGuardConfig
from ai_game_player.runtime_log import RuntimeLog


# {
#   責務: [DecisionPipeline: 画面観測から候補判断・安全評価・実行までを接続する]
#   フィールド: [source: 観測入力, executor: 安全な候補実行境界, controller: 実行制御]
#   処理: [対象識別情報を実行境界まで引き継ぎ、実行結果と履歴を管理する]
#   フィールド: [event_journal: session eventのappend先, runtime_log: active Journalへruntime eventを複製するoptional logger, _runtime_log_attachment_token: このPipelineだけがbindingを解除する所有token]
# }
class DecisionPipeline:
    # {
    #   責務: [
    #     __init__: observation・判断・実行コンポーネントを対象識別情報付きで接続する
    #   ]
    #   処理: [
    #     1: 候補生成と判断用componentを初期化する
    #     2: HWND・PID・入力方式をActionExecutorへ渡す
    #     3: RunControllerの停止状態を実入力の停止確認へ渡す
    #     4: 自動カーソル通知関数をActionExecutorへ渡す
    #     5: Session履歴・安全評価・RuntimeLog bindingを先に初期化し、失敗時にexecutorを残さない
    #     6: すべての履歴adapter初期化後にActionExecutorを生成する
    #   ]
    #   引数: [
    #     source: 判断対象の画面観測と操作候補を提供する入力元
    #     game_directory: ゲーム固有の履歴・安全状態を保存するdirectory
    #     provider: 操作候補から次の判断を選ぶProvider
    #     controller: stop/rearm世代を共有するRunController
    #     dry_run: trueなら入力を送らず実行内容だけを記録する設定
    #     window_handle: 入力対象のHWND
    #     input_mode: OS mouseまたはwindow messageの入力方式
    #     window_process_id: 対象HWNDを所有するprocess ID
    #     safety_evaluator: 操作候補のリスクを評価するoptional evaluator
    #     safety_guard: 実入力を監視するoptional SafetyGuard
    #     safety_guard_config: SafetyGuardの閾値と制限
    #     emergency_stop: OS入力を停止するoptional emergency stop
    #     fail_safe_runtime: persistent fail-safe状態を管理するoptional runtime
    #     fail_safe_config: fail-safeの制限と復旧設定
    #     external_watchdog: trueなら外部watchdog processを起動する設定
    #     automated_cursor_position_callback: 自動入力workerの開始・完了位置を通知する関数
    #     event_journal: このSession専用のevent保存先。未指定ならSession Journalを使わない
    #     runtime_log: application log。Journal指定時はeventを同じSessionへ複製する
    #     migrate_legacy: trueなら旧history・trace・execution・safety記録をJournalへ移行する
    #     migrate_runtime_log: trueなら旧RuntimeLog JSONLをJournalへ移行する
    #   ]
    #   戻り値: []
    # }
    def __init__(
        self,
        source: ObservationSource,
        game_directory: Path,
        provider: object | None = None,
        controller: RunController | None = None,
        dry_run: bool = True,
        window_handle: int | None = None,
        input_mode: str = "mouse",
        window_process_id: int | None = None,
        safety_evaluator: ActionSafetyEvaluator | None = None,
        safety_guard: SafetyGuard | None = None,
        safety_guard_config: SafetyGuardConfig | None = None,
        emergency_stop: EmergencyStop | None = None,
        fail_safe_runtime: FailSafeRuntime | None = None,
        fail_safe_config: FailSafeConfig | None = None,
        external_watchdog: bool = True,
        automated_cursor_position_callback: Callable[[tuple[int, int] | None, bool], None] | None = None,
        event_journal: EventJournal | None = None,
        runtime_log: RuntimeLog | None = None,
        migrate_legacy: bool = True,
        migrate_runtime_log: bool = True,
    ) -> None:
        self.source = source
        self.ocr = OcrTextCandidateDetector()
        self.merger = CandidateMerger()
        self.event_journal = event_journal
        self.runtime_log = runtime_log
        self._runtime_log_attachment_token: object | None = None
        self._runtime_log_detached = runtime_log is None or event_journal is None
        self._event_journal_closed = event_journal is None
        self._executor_closed = False
        self.engine = GamePlayerEngine(
            game_directory,
            provider,
            event_journal=event_journal,
            migrate_legacy=migrate_legacy,
        )
        self.controller = controller or RunController()
        self._seen_rearm_token = 0
        execution_history_path = game_directory / "execution_history.json"
        self.execution_history = ExecutionHistory(
            execution_history_path,
            LegacyEventAdapter(
                event_journal,
                execution_history_path,
                "execution.result",
                migrate_legacy=migrate_legacy,
            )
            if event_journal is not None else None,
        )
        self.safety_evaluator = safety_evaluator or ActionSafetyEvaluator()
        safety_audit_path = game_directory / "action_safety.json"
        self.safety_audit = ActionSafetyAuditLog(
            safety_audit_path,
            LegacyEventAdapter(
                event_journal,
                safety_audit_path,
                "safety.audit",
                migrate_legacy=migrate_legacy,
            )
            if event_journal is not None else None,
        )
        self.last_safety_result: ActionSafetyResult | None = None
        if self.event_journal is not None and self.runtime_log is not None:
            self._runtime_log_attachment_token = self.runtime_log.attach_event_journal(
                self.event_journal,
                migrate_legacy=migrate_runtime_log,
            )
        try:
            self.executor = ActionExecutor(
                dry_run,
                window_handle=window_handle,
                window_process_id=window_process_id,
                input_mode=input_mode,
                safety_guard=safety_guard,
                safety_config=safety_guard_config,
                safety_log_path=game_directory / "safety_guard.jsonl",
                emergency_stop=emergency_stop,
                fail_safe_runtime=fail_safe_runtime,
                fail_safe_config=fail_safe_config,
                fail_safe_state_directory=game_directory / "fail_safe",
                external_watchdog=external_watchdog,
                run_controller_stop_checker=lambda: not self.controller.is_running,
                automated_cursor_position_callback=automated_cursor_position_callback,
            )
        except Exception:
            if self._runtime_log_attachment_token is not None and self.runtime_log is not None:
                self.runtime_log.detach_event_journal(self._runtime_log_attachment_token)
                self._runtime_log_detached = True
            raise

    def _read_candidates(self, ocr_texts: list[dict[str, object]] | None = None) -> tuple[ScreenObservation, list[ActionCandidate]]:
        observation, configured = self.source.read()
        runtime = self.executor.fail_safe_runtime
        if runtime is not None and runtime.state == FailSafeState.ACTIVE:
            self.executor.record_observation()
        detected = self.ocr.detect(observation, ocr_texts if ocr_texts is not None else observation.features.get("ocr_candidates", []))
        image = [ActionCandidate.from_dict(value) for value in observation.features.get("image_candidates", []) if isinstance(value, dict)]
        return observation, self.merger.merge(configured, detected, image)

    # {
    #   責務: [_decide: 候補を判断し、推論応答を履歴へ確定する前に停止状態を再検証する]
    #   処理: [観測候補を取得してproviderを呼び、履歴公開をrun世代lock内で行うguardをengineへ渡す]
    #   引数: [ocr_texts: 画面から認識したOCR候補, purpose: 判断providerへ渡すゲーム目標, personality: 判断方針, expected_rearm_token: 判断開始時の再開世代]
    #   戻り値: [ActionDecision・候補・ScreenObservation: 同一snapshotの判断情報]
    # }
    def _decide(
        self,
        ocr_texts: list[dict[str, object]] | None = None,
        purpose: str = "",
        personality: str = "",
        expected_rearm_token: int | None = None,
    ) -> tuple[ActionDecision, list[ActionCandidate], ScreenObservation]:
        observation, candidates = self._read_candidates(ocr_texts)
        decision, _ = self.engine.step_with_candidate(
            observation,
            candidates,
            purpose,
            personality,
            before_provider=lambda: self.controller.ensure_running(expected_rearm_token),
            commit_guard=lambda commit: self.controller.run_if_current(expected_rearm_token, commit),
        )
        return decision, candidates, observation

    # {
    #   責務: [run: 実行世代を固定して判断し、停止後に返った推論結果を破棄する]
    #   処理: [開始時の世代を検証し、判断後にも同世代の実行許可を確認する]
    #   引数: [expected_rearm_token: 呼び出し元が保持する実行開始時の世代]
    #   戻り値: [ActionDecision: 有効な実行世代で得た判断]
    #   エラー: [ExecutionCancelled: 停止後または再開前の推論結果]
    # }
    def run(
        self,
        ocr_texts: list[dict[str, object]] | None = None,
        purpose: str = "",
        personality: str = "",
        expected_rearm_token: int | None = None,
    ) -> ActionDecision:
        run_token = self.controller.rearm_token if expected_rearm_token is None else expected_rearm_token
        self.controller.ensure_running(run_token)
        self._sync_runtime_rearm()
        decision, _, _ = self._decide(ocr_texts, purpose, personality, run_token)
        self.controller.ensure_running(run_token)
        return decision

    # {
    #   責務: [run_and_execute: 同じ判断snapshotで許可された候補だけを停止確認後に実行する]
    #   処理: [画面候補・判断・評価済み候補を固定し、実入力前に停止世代とSafety評価を再検証する]
    #   引数: [ocr_texts: 判断へ追加するOCR候補, purpose: providerへ渡すゲーム目標, personality: providerへ渡す判断方針, expected_rearm_token: 呼び出し元が保持する実行開始時の世代]
    #   戻り値: [ExecutionResult: 選択した候補の実行結果]
    #   エラー: [停止・再開で世代が失効した場合はExecutionCancelled、安全評価が拒否した場合はRuntimeError]
    # }
    def run_and_execute(
        self,
        ocr_texts: list[dict[str, object]] | None = None,
        purpose: str = "",
        personality: str = "",
        expected_rearm_token: int | None = None,
    ) -> ExecutionResult:
        run_token = self.controller.rearm_token if expected_rearm_token is None else expected_rearm_token
        self.controller.ensure_running(run_token)
        self._sync_runtime_rearm()
        observation, candidates = self._read_candidates(ocr_texts)
        _, selected = self.engine.step_with_candidate(
            observation,
            candidates,
            purpose,
            personality,
            before_provider=lambda: self.controller.ensure_running(run_token),
            commit_guard=lambda commit: self.controller.run_if_current(run_token, commit),
        )
        self.controller.ensure_running(run_token)

        # 評価・判断済みオブジェクトを維持し、後段で同じIDの別候補へ置き換えない。
        safety_context, snapshot_id = self._safety_context(selected.action_id, purpose)
        assessment = self.safety_evaluator.evaluate(observation, selected, safety_context)
        self.last_safety_result = assessment
        self.safety_audit.append_evaluation(assessment, snapshot_id=snapshot_id, goal=purpose)
        if assessment.status == SafetyStatus.BLOCK:
            raise RuntimeError(f"Action Safety Evaluator blocked action: {selected.action_id}")
        if assessment.requires_verification and not self.executor.dry_run:
            requests = ", ".join(assessment.verification_requests)
            raise RuntimeError(f"Action Safety Evaluator requires verification before live input: {requests}")

        self.controller.ensure_running(run_token)
        self._bind_execution_stop_checker(run_token)
        result = self.executor.execute(selected)
        self.execution_history.append(result)
        self.safety_audit.append_execution(assessment.assessment_id, result)
        return result

    # {
    #   責務: [load_execution_history: pipelineが現在のlegacy互換形で保持する実行履歴を返す]
    #   引数: [なし]
    #   戻り値: [list[ExecutionResult]: 旧保存分とSession Journalの実行結果]
    # }
    def load_execution_history(self) -> list[ExecutionResult]:
        return self.execution_history.load()

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

    # {
    #   責務: [close: 入力executor・runtime log binding・Session Journalを終了する]
    #   処理: [executor・runtime log・Journalを順番に閉じ、各cleanup errorを集めて送出する]
    #   引数: [self: 終了するDecisionPipeline]
    #   戻り値: []
    #   エラー: [executorまたはJournal closeで発生した終了errorを呼出元へ送る]
    # }
    def close(self) -> None:
        close_errors: list[Exception] = []
        try:
            self.close_execution_resources()
        except Exception as error:
            close_errors.append(error)
        try:
            self.close_event_journal()
        except Exception as error:
            close_errors.append(error)
        if close_errors:
            raise RuntimeError("Pipeline cleanup failed: " + "; ".join(map(str, close_errors))) from close_errors[0]

    # {
    #   責務: [close_execution_resources: 実入力executorを停止し失敗をJournalへ記録する]
    #   処理: [executor終了後もJournalを開いたままにして、後続Provider終了失敗を保存できるようにする]
    #   引数: [なし]
    #   戻り値: []
    #   エラー: [Exception: executor終了またはJournalへの失敗記録に失敗した場合]
    # }
    def close_execution_resources(self) -> None:
        if self._executor_closed:
            return
        try:
            self.executor.close()
            self._executor_closed = True
        except Exception as error:
            try:
                self.record_shutdown_failure("executor", error)
            except Exception as logging_error:
                raise RuntimeError(
                    f"Executor shutdown failed ({error}); failure logging also failed ({logging_error})"
                ) from error
            raise

    # {
    #   責務: [close_event_journal: RuntimeLogのSession bindingとSession Journalを閉じる]
    #   処理: [RuntimeLog bindingを保持したまま事前checkpointし、失敗を記録してからJournalを閉じ最後にdetachする]
    #   引数: [なし]
    #   戻り値: []
    #   エラー: [Exception: SQLite checkpointまたは接続closeに失敗した場合]
    # }
    def close_event_journal(self) -> None:
        close_error: Exception | None = None
        if not self._event_journal_closed and self.event_journal is not None:
            try:
                self.event_journal.flush()
            except Exception as error:
                close_error = error
                if self.runtime_log is not None:
                    try:
                        self.runtime_log.write(
                            "session.shutdown_failed",
                            "Session Event Journal checkpoint failed before close",
                            {
                                "status": "failed",
                                "resource": "event_journal_checkpoint",
                                "error": str(error),
                            },
                        )
                    except Exception:
                        pass
            try:
                self.event_journal.close()
                self._event_journal_closed = True
            except Exception as error:
                if close_error is None:
                    close_error = error
                if self.runtime_log is not None:
                    try:
                        self.runtime_log.write(
                            "session.shutdown_failed",
                            "Session Event Journal close failed",
                            {
                                "status": "failed",
                                "resource": "event_journal_close",
                                "error": str(error),
                            },
                        )
                    except Exception:
                        pass
        if not self._runtime_log_detached and self.runtime_log is not None:
            if self._runtime_log_attachment_token is not None:
                self.runtime_log.detach_event_journal(self._runtime_log_attachment_token)
            self._runtime_log_detached = True
        if close_error is not None:
            raise close_error

    # {
    #   責務: [record_shutdown_failure: Provider・executorの終了失敗をJournalへ記録する]
    #   処理: [Journalを閉じる前にruntime logへresource名と例外内容を追記する]
    #   引数: [resource: 終了に失敗したresource名, error: resource closeが送出した例外]
    #   戻り値: []
    #   エラー: [OSErrorまたはEventJournalError: JSONLまたはJournalへ失敗記録を書けない場合]
    # }
    def record_shutdown_failure(self, resource: str, error: Exception) -> None:
        if self.runtime_log is None:
            return
        self.runtime_log.write(
            "session.shutdown_failed",
            f"Session shutdown failed while closing {resource}",
            {"status": "failed", "resource": resource, "error": str(error)},
        )

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
    #   責務: [_bind_execution_stop_checker: 現在の実入力をRunControllerの停止・再開世代へ結び付ける]
    #   処理: [停止状態または開始後の世代変更を検出する確認関数をActionExecutorへ登録する]
    #   引数: [run_token: 実入力stepの開始時に記録した再開世代]
    #   戻り値: []
    # }
    def _bind_execution_stop_checker(self, run_token: int) -> None:
        self.executor.run_controller_stop_checker = lambda: (
            not self.controller.is_running or self.controller.rearm_token != run_token
        )

    def _safety_context(self, action_id: str, purpose: str) -> tuple[SafetyEvaluationContext, str]:
        recent = self.engine.trace.recent(1)
        if not recent:
            return SafetyEvaluationContext(current_goal=purpose), ""
        entry = recent[-1]
        raw_context = entry.get("context", {})
        if not isinstance(raw_context, dict):
            return SafetyEvaluationContext(current_goal=purpose), str(entry.get("snapshot_id", ""))
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
            ),
            str(raw_context.get("snapshot_id", entry.get("snapshot_id", ""))),
        )
