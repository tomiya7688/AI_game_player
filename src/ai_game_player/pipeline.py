from dataclasses import replace
from pathlib import Path
from datetime import datetime, timezone
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
from ai_game_player.decision_verifier import ReliabilityStatus
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.execution_history import ExecutionHistory
from ai_game_player.execution_receipt import ExecutionObservation, ExecutionReceipt
from ai_game_player.event_journal import EventJournal
from ai_game_player.fail_safe_runtime import (
    FailSafeConfig,
    FailSafeRuntime,
    FailSafeState,
)
from ai_game_player.journal_adapters import LegacyEventAdapter
from ai_game_player.knowledge_feature_adapter import KnowledgeFeatureAdapter
from ai_game_player.runtime_evaluation_adapter import RuntimeEvaluationAdapter
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.observation_source import ObservationSource
from ai_game_player.ocr_detector import OcrTextCandidateDetector
from ai_game_player.run_control import ExecutionCancelled, RunController
from ai_game_player.safety_guard import EmergencyStop, SafetyGuard, SafetyGuardConfig
from ai_game_player.runtime_log import RuntimeLog


# {
#   責務: [DecisionPipeline: 画面観測から検証済み判断、安全評価、世代guard付き実行までを接続する]
#   フィールド: [source: 画面と候補を返す入力元, engine: Decision Context作成と回答検証, safety_evaluator: 操作リスク評価, executor: dry-runまたはOS入力, controller: 停止・再開世代, event_journal: 任意のSession event保存先]
#   処理: [判断snapshotに含まれる候補実体を実行まで保ち、Action SafetyとDecision Reliabilityを別の根拠として記録する。game adapterが新しい画面の観測完了時刻をFailSafeRuntimeへ記録する入口も提供する]
#   履歴: [指定Journalへ判断・実行・安全eventを追記し、RuntimeLog bindingはこのPipelineが所有するtokenで解除する]
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
    #     knowledge_adapter: 選択済みKnowledgeをstep固定で投影する。Safetyや実入力許可は変更しない
    #     evaluation_adapter: 通常推論で6軸を投影する既定部品。ModeのArtifact評価とは別
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
        automated_cursor_position_callback: Callable[
            [tuple[int, int] | None, bool], None
        ]
        | None = None,
        event_journal: EventJournal | None = None,
        runtime_log: RuntimeLog | None = None,
        migrate_legacy: bool = True,
        migrate_runtime_log: bool = True,
        knowledge_adapter: KnowledgeFeatureAdapter | None = None,
        evaluation_adapter: RuntimeEvaluationAdapter | None = None,
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
            knowledge_adapter=knowledge_adapter,
            evaluation_adapter=evaluation_adapter,
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
            if event_journal is not None
            else None,
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
            if event_journal is not None
            else None,
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
            if (
                self._runtime_log_attachment_token is not None
                and self.runtime_log is not None
            ):
                self.runtime_log.detach_event_journal(
                    self._runtime_log_attachment_token
                )
                self._runtime_log_detached = True
            raise

    # {
    #   責務: [record_observation: 新しい画面の観測完了時刻をFailSafeRuntimeへ記録する]
    #   処理: [game adapterが画面の状態取得を終えた直後に呼び出し、ActionExecutorへ時刻更新を委譲する]
    #   引数: []
    #   戻り値: [None: 結果値を返さない]
    #   エラー: [RuntimeError: FailSafeRuntimeが観測時刻の更新を受理しない場合]
    # }
    def record_observation(self) -> None:
        self.executor.record_observation()

    # {
    #   責務: [_read_candidates: 画面観測と設定済み候補から判断用の候補一覧を作る]
    #   処理: [1: sourceから画面と設定済み候補を取得する, 2: FailSafeRuntimeが実行中なら観測完了時刻を更新する, 3: OCR候補と画像候補を設定済み候補へ統合する]
    #   引数: [ocr_texts: 呼び出し側が指定するOCR結果。NoneならScreenObservationのocr_candidatesを使用する]
    #   戻り値: [ScreenObservationと統合済みActionCandidate一覧の組]
    #   エラー: [source、OCR検出、候補変換の処理で発生した例外を呼び出し元へ伝える]
    # }
    def _read_candidates(
        self, ocr_texts: list[dict[str, object]] | None = None
    ) -> tuple[ScreenObservation, list[ActionCandidate]]:
        observation, configured = self.source.read()
        runtime = self.executor.fail_safe_runtime
        if runtime is not None and runtime.state == FailSafeState.ACTIVE:
            self.record_observation()
        detected = self.ocr.detect(
            observation,
            ocr_texts
            if ocr_texts is not None
            else observation.features.get("ocr_candidates", []),
        )
        image = [
            ActionCandidate.from_dict(value)
            for value in observation.features.get("image_candidates", [])
            if isinstance(value, dict)
        ]
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
            before_provider=lambda: self.controller.ensure_running(
                expected_rearm_token
            ),
            commit_guard=lambda commit: self.controller.run_if_current(
                expected_rearm_token, commit
            ),
            action_generation=expected_rearm_token,
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
        run_token = (
            self.controller.rearm_token
            if expected_rearm_token is None
            else expected_rearm_token
        )
        self.controller.ensure_running(run_token)
        self._sync_runtime_rearm()
        decision, _, _ = self._decide(ocr_texts, purpose, personality, run_token)
        self.controller.ensure_running(run_token)
        return decision

    # {
    #   責務: [run_and_execute: 現在画面で選んだ候補を信頼性・Action Safety・停止世代の条件に従って実行する]
    #   処理: [同一snapshotの候補実体を固定し、Safety BLOCKまたは実入力前の追加確認が残る場合は履歴を停止扱いにして入力しない]
    #   引数: [ocr_texts: 観測へ加えるOCR候補, purpose: ゲーム内達成目標, personality: provider応答方針, expected_rearm_token: 判断開始時の実行世代]
    #   戻り値: [ExecutionResult: dry-runまたは入力executorが返した実行結果]
    #   エラー: [RuntimeError: Safety条件が実行を拒否した場合, ExecutionCancelled: 判断または入力の途中で実行世代が失効した場合]
    # }
    def run_and_execute(
        self,
        ocr_texts: list[dict[str, object]] | None = None,
        purpose: str = "",
        personality: str = "",
        expected_rearm_token: int | None = None,
    ) -> ExecutionResult:
        run_token = (
            self.controller.rearm_token
            if expected_rearm_token is None
            else expected_rearm_token
        )
        self.controller.ensure_running(run_token)
        self._sync_runtime_rearm()
        observation, candidates = self._read_candidates(ocr_texts)
        if not self.executor.dry_run:
            self.complete_execution_observation(
                observation, expected_rearm_token=run_token
            )
        _, selected = self.engine.step_with_candidate(
            observation,
            candidates,
            purpose,
            personality,
            before_provider=lambda: self.controller.ensure_running(run_token),
            commit_guard=lambda commit: self.controller.run_if_current(
                run_token, commit
            ),
            action_generation=run_token,
        )
        self.controller.ensure_running(run_token)
        reliability_result = self.engine.last_reliability_result
        reliability_record = (
            reliability_result.to_dict() if reliability_result is not None else None
        )
        safety_context, snapshot_id = self._safety_context(
            selected.action_id, purpose, reliability_record
        )
        assessment = self.safety_evaluator.evaluate(
            observation, selected, safety_context
        )
        self.last_safety_result = assessment
        self.safety_audit.append_evaluation(
            assessment, snapshot_id=snapshot_id, goal=purpose
        )
        if assessment.status == SafetyStatus.BLOCK:
            self.engine.mark_last_decision_not_executed(
                f"Action Safety Evaluator blocked action: {selected.action_id}"
            )
            raise RuntimeError(
                f"Action Safety Evaluator blocked action: {selected.action_id}"
            )
        if assessment.requires_verification and not self.executor.dry_run:
            requests = ", ".join(assessment.verification_requests)
            self.engine.mark_last_decision_not_executed(
                f"Action Safety verification required: {requests}"
            )
            raise RuntimeError(
                f"Action Safety Evaluator requires verification before live input: {requests}"
            )
        if not self.executor.dry_run and reliability_result is None:
            raise RuntimeError(
                "Decision Verifier did not produce a reliability result; live input is blocked"
            )
        if (
            not self.executor.dry_run
            and reliability_result is not None
            and reliability_result.status != ReliabilityStatus.TRUST
        ):
            self.engine.mark_last_decision_not_executed(
                f"Decision Reliability requires verification: {reliability_result.status.value}"
            )
            raise RuntimeError(
                "Decision Reliability requires verification before live input: "
                f"{reliability_result.status.value}"
            )

        self.controller.ensure_running(run_token)
        self._bind_execution_stop_checker(run_token)
        try:
            result = self.executor.execute(selected)
        except Exception as error:
            dispatch = getattr(self.executor, "input_dispatch_started", None)
            status = "not_executed" if dispatch is False else "dispatch_unknown"
            self._record_execution_receipt(
                observation,
                selected.action_id,
                snapshot_id,
                run_token,
                status,
                dispatch,
                type(error).__name__,
            )
            raise
        dispatch = getattr(self.executor, "input_dispatch_started", None)
        if result.mode == "dry_run":
            status, dispatch = "dry_run", False
        elif dispatch is False:
            status = "not_dispatched" if result.executed else "not_executed"
        elif result.executed:
            status, dispatch = "executed", True
        else:
            status = "dispatch_unknown"
        try:
            self._record_execution_receipt(
                observation,
                selected.action_id,
                snapshot_id,
                run_token,
                status,
                dispatch,
                result.mode,
            )
        finally:
            self.execution_history.append(result)
            self.safety_audit.append_execution(assessment.assessment_id, result)
        return result

    # {
    #   責務: [_record_execution_receipt: Executor結果を同じ判断snapshot・実行世代へ結び付ける]
    #   処理: [現在世代ならpending更新を許可し、停止済み世代は学習不可のauditだけへ保存する]
    #   引数: [observation: 入力直前画面, action_id/snapshot_id: 検証済み判断参照,
    #     generation: 実行開始世代, status/dispatch: Executorの送信証拠, reason: 固定監査理由]
    #   戻り値: [なし]
    #   エラー: [ExecutionCancelled: 停止・再開で実行世代が変わった, RuntimeError: traceの不整合]
    # }
    def _record_execution_receipt(
        self,
        observation: ScreenObservation,
        action_id: str,
        snapshot_id: str,
        generation: int,
        status: str,
        dispatch: bool | None,
        reason: str,
    ) -> None:
        if not snapshot_id:
            if status in {"dry_run", "not_executed", "not_dispatched"}:
                return
            raise RuntimeError("actual execution requires a bound decision snapshot")
        receipt = ExecutionReceipt(
            snapshot_id,
            action_id,
            generation,
            observation.screen_id,
            str(observation.features.get("signature", "")),
            status,
            dispatch,
            reason=reason,
        )

        # {
        #   責務: [register_current: 現世代の受領事実と入力前停止の監査を一度に保存する]
        #   処理: [同じguard内で元判断だけを更新し、停止/rearm後の別判断を停止扱いにしない]
        #   引数: [なし]
        #   戻り値: [なし]
        #   エラー: [RuntimeError: 元traceの参照不整合]
        # }
        def register_current() -> None:
            self.engine.record_execution_receipt(observation, receipt)
            if status == "not_executed":
                self.engine.mark_last_decision_not_executed(
                    f"OS入力を開始する前に実行器が失敗しました: {reason}"
                )

        try:
            self.controller.run_if_current(generation, register_current)
        except ExecutionCancelled:
            stale_receipt = replace(receipt, eligible_for_learning=False)
            self.controller.record_execution_audit(
                lambda: self.engine.record_execution_receipt(
                    observation, stale_receipt, activate=False
                )
            )
            raise

    # {
    #   責務: [complete_execution_observation: 明示された実操作後の画面を同世代のpending操作へ帰属する]
    #   処理: [dry-runを除外し、検出後にも世代を検査してOutcomeを一度だけ公開する]
    #   引数: [observation: post-actionの実画面, expected_rearm_token: 観測開始時の実行世代。Noneは現在世代]
    #   戻り値: [ExecutionObservationまたはNone: receiptと元の実操作結果。pendingが無い場合None]
    #   エラー: [ExecutionCancelled: 観測中に停止・再開した]
    # }
    def complete_execution_observation(
        self, observation: ScreenObservation, *, expected_rearm_token: int | None = None
    ) -> ExecutionObservation | None:
        token = (
            self.controller.rearm_token
            if expected_rearm_token is None
            else expected_rearm_token
        )
        self.controller.ensure_running(token)
        if self.executor.dry_run:
            return None
        return self.engine.complete_execution_observation(
            observation,
            token,
            commit_guard=lambda commit: self.controller.run_if_current(token, commit),
        )

    # {
    #   責務: [load_execution_history: pipelineが現在のlegacy互換形で保持する実行履歴を返す]
    #   引数: [なし]
    #   戻り値: [list[ExecutionResult]: 旧保存分とSession Journalの実行結果]
    # }
    def load_execution_history(self) -> list[ExecutionResult]:
        return self.execution_history.load()

    def record_safety_outcome(
        self, status: str, confidence: float, evidence: str = ""
    ) -> None:
        if self.last_safety_result is None:
            raise RuntimeError("no action safety assessment is available")
        self.safety_audit.append_outcome(
            self.last_safety_result.assessment_id, status, confidence, evidence
        )
        normalized = status.casefold()
        changed = (
            normalized in {"ongoing", "success"}
            or "screen signature changed" in evidence.casefold()
        )
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
            raise RuntimeError(
                "Pipeline cleanup failed: " + "; ".join(map(str, close_errors))
            ) from close_errors[0]

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
    #   処理: [RuntimeLog bindingを保持したまま事前checkpointし、失敗をRuntimeLogまたは未閉鎖Journalへ記録してからJournalを閉じ最後にdetachする。closeがcheckpoint errorを送出してもJournalはclosedとして扱う]
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
                try:
                    self.record_shutdown_failure("event_journal_checkpoint", error)
                except Exception:
                    pass
            try:
                self.event_journal.close()
            except Exception as error:
                if close_error is None:
                    close_error = error
                try:
                    self.record_shutdown_failure("event_journal_close", error)
                except Exception:
                    pass
            finally:
                # EventJournalは最終checkpoint失敗時もconnectionを解放してclosed状態になる。
                self._event_journal_closed = True
        if not self._runtime_log_detached and self.runtime_log is not None:
            if self._runtime_log_attachment_token is not None:
                self.runtime_log.detach_event_journal(
                    self._runtime_log_attachment_token
                )
            self._runtime_log_detached = True
        if close_error is not None:
            raise close_error

    # {
    #   責務: [record_shutdown_failure: Provider・executorの終了失敗をJournalへ記録する]
    #   処理: [Journalを閉じる前にruntime logへ追記し、runtime logがなければ同じrecordをSession Journalへ直接appendする]
    #   引数: [resource: 終了に失敗したresource名, error: resource closeが送出した例外]
    #   戻り値: []
    #   エラー: [OSErrorまたはEventJournalError: 保存先が一つだけ失敗した場合の元の例外, RuntimeError: JSONLとJournalの両方が失敗した場合]
    # }
    def record_shutdown_failure(self, resource: str, error: Exception) -> None:
        event_name = "session.shutdown_failed"
        message = f"Session shutdown failed while closing {resource}"
        context = {"status": "failed", "resource": resource, "error": str(error)}
        if self.runtime_log is not None:
            self.runtime_log.write(event_name, message, context)
            return
        if self.event_journal is not None:
            self.event_journal.append(
                "runtime.log",
                status="failed",
                payload={
                    "legacy_record": {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": event_name,
                        "message": message,
                        "context": context,
                    }
                },
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
    #   責務: [_bind_execution_stop_checker: 実入力中に現在のRunController世代を監視する]
    #   処理: [開始時の世代が変わるか停止状態になった場合にTrueを返す関数をActionExecutorへ登録する]
    #   引数: [run_token: 現在の実行stepが開始時に保持した再開世代]
    #   戻り値: [なし。ActionExecutorの停止確認を現在のrun世代へ更新する]
    # }
    def _bind_execution_stop_checker(self, run_token: int) -> None:
        self.executor.run_controller_stop_checker = lambda: (
            not self.controller.is_running or self.controller.rearm_token != run_token
        )

    # {
    #   責務: [_safety_context: 同じ判断traceから目的寄与点と信頼性根拠をSafety Evaluatorへ渡す]
    #   処理: [指定actionの評価値を読み、Action Safety用のcontextとsnapshot IDを組み立てる]
    #   引数: [action_id: Safety評価する候補ID, purpose: 判断時に利用者が指定したゲーム目標, decision_reliability: 同じ選択のDecision Verifier結果。未取得ならNone]
    #   戻り値: [tuple[SafetyEvaluationContext, str]: Safety評価入力と根拠となるsnapshot ID]
    # }
    def _safety_context(
        self,
        action_id: str,
        purpose: str,
        decision_reliability: dict[str, object] | None = None,
    ) -> tuple[SafetyEvaluationContext, str]:
        recent = self.engine.trace.recent(1)
        if not recent:
            return SafetyEvaluationContext(
                current_goal=purpose, decision_reliability=decision_reliability
            ), ""
        entry = recent[-1]
        raw_context = entry.get("context", {})
        if not isinstance(raw_context, dict):
            return (
                SafetyEvaluationContext(
                    current_goal=purpose, decision_reliability=decision_reliability
                ),
                str(entry.get("snapshot_id", "")),
            )
        utility_score = None
        utility_confidence = None
        candidates = raw_context.get("candidates", [])
        if isinstance(candidates, list):
            for candidate in candidates:
                if (
                    not isinstance(candidate, dict)
                    or str(candidate.get("action_id", "")) != action_id
                ):
                    continue
                evaluation = candidate.get("evaluation", {})
                if isinstance(evaluation, dict):
                    score = evaluation.get("score")
                    confidence = evaluation.get("confidence")
                    utility_score = (
                        float(score) if isinstance(score, (int, float)) else None
                    )
                    utility_confidence = (
                        float(confidence)
                        if isinstance(confidence, (int, float))
                        else None
                    )
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
