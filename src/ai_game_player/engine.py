from dataclasses import replace
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

from ai_game_player.decision_context import (
    DecisionContext,
    DecisionContextBuilder,
    DecisionTraceStore,
)
from ai_game_player.decision_verifier import (
    DecisionVerifier,
    ReliabilityResult,
    ReliabilityStatus,
)
from ai_game_player.atomic_json import StagedJsonWrite
from ai_game_player.deferred_trace_append import DeferredTraceAppend
from ai_game_player.event_journal import EventJournal
from ai_game_player.execution_receipt import ExecutionObservation, ExecutionReceipt
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.history import HistoryStore
from ai_game_player.journal_adapters import LegacyEventAdapter, StagedJournalAppend
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.outcome_fusion import OutcomeDetector
from ai_game_player.outcome_models import OutcomeEvent
from ai_game_player.provider import RuleProvider


_BEFORE_PROVIDER_CONTEXT: ContextVar[Callable[[], None] | None] = ContextVar(
    "before_provider", default=None
)
_COMMIT_GUARD_CONTEXT: ContextVar[Callable[[Callable[[], None]], None] | None] = (
    ContextVar("commit_guard", default=None)
)
_ACTION_GENERATION_CONTEXT: ContextVar[int | None] = ContextVar(
    "action_generation", default=None
)
_ALLOWED_CANDIDATES_CONTEXT: ContextVar[tuple[ActionCandidate, ...] | None] = (
    ContextVar("allowed_candidates", default=None)
)


# {
#   責務: [GamePlayerEngine: 許可候補の判断・信頼性検査・実操作Outcome追跡とJournal保存を管理する]
#   フィールド: [decision_verifier: provider回答を画面と候補へ照合する検査器, trace: snapshotに判断根拠と実行結果を結ぶ追記履歴, event_journal: 指定時に履歴を保存するSession Journal]
#   処理: [停止世代guard内で履歴を公開し、planned判断と実行受領記録を分離し、実操作後の明示観測だけを同世代へ帰属する]
# }
class GamePlayerEngine:
    # {
    #   責務: [__init__: ゲーム保存先・判断部品・任意Session Journalを接続する]
    #   処理: [旧history/traceを必要時だけJournalへ移行し、検証器とprevious action状態を初期化する]
    #   引数: [game_directory: ゲーム別保存先, provider: 判断provider, context_builder: 判断材料作成器, outcome_detector: 操作結果検出器, event_journal: 現Sessionのevent保存先, migrate_legacy: 旧recordを初回Sessionへ移行するか]
    #   戻り値: [なし。構成部品を以後のstepで利用できる状態にする]
    # }
    def __init__(
        self,
        game_directory: Path,
        provider=None,
        context_builder: DecisionContextBuilder | None = None,
        outcome_detector: OutcomeDetector | None = None,
        event_journal: EventJournal | None = None,
        migrate_legacy: bool = True,
    ) -> None:
        self.evaluator = ActionEvaluator()
        self.provider = provider or RuleProvider()
        history_path = game_directory / "history.json"
        trace_path = game_directory / "decision_trace.json"
        self.history = HistoryStore(
            history_path,
            LegacyEventAdapter(
                event_journal,
                history_path,
                "decision.history",
                migrate_legacy=migrate_legacy,
            )
            if event_journal is not None
            else None,
        )
        self.trace = DecisionTraceStore(
            trace_path,
            LegacyEventAdapter(
                event_journal,
                trace_path,
                "decision.trace",
                migrate_legacy=migrate_legacy,
            )
            if event_journal is not None
            else None,
        )
        self.context_builder = context_builder or DecisionContextBuilder(
            KnowledgeStore(game_directory / "knowledge.json")
        )
        self.outcome_detector = outcome_detector or OutcomeDetector(
            self._semantic_outcome_provider()
        )
        self.decision_verifier = DecisionVerifier()
        self.last_reliability_result: ReliabilityResult | None = None
        self.last_outcome_event: OutcomeEvent | None = None
        self.last_execution_observation: ExecutionObservation | None = None
        self._previous_observation: ScreenObservation | None = None
        self._previous_action_id: str | None = None
        self._last_decision_trace_reference: tuple[str, str] | None = None
        self._previous_action_trace_reference: tuple[str, str] | None = None
        self._pending_execution_receipt: ExecutionReceipt | None = None
        self._pending_outcome_observed = False

    # {
    #   責務: [step: 許可候補から判断し、検査結果と停止世代を考慮した履歴を公開する]
    #   処理: [判断context作成、provider応答検証、traceと履歴の準備、世代guard内commitを順に行う]
    #   引数: [observation: 候補を照合する判断時点の画面, candidates: 検出された操作候補, purpose: 利用者の達成目標, personality: providerへ渡す応答方針]
    #   戻り値: [ActionDecision: 検証に通過した許可候補と理由]
    #   エラー: [ValueError: 回答が拒否または許可候補外の場合, ExecutionCancelled: 停止で実行世代が失効した場合]
    # }
    def step(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
    ) -> ActionDecision:
        self.last_reliability_result = None
        before_provider = _BEFORE_PROVIDER_CONTEXT.get()
        commit_guard = _COMMIT_GUARD_CONTEXT.get()
        if before_provider is not None:
            before_provider()
        candidate_snapshot = _ALLOWED_CANDIDATES_CONTEXT.get()
        allowed_snapshot = (
            tuple(candidate_snapshot)
            if candidate_snapshot is not None
            else tuple(self.evaluator.evaluate(observation, candidates))
        )
        allowed = list(allowed_snapshot)
        previous_outcome = self._assess_previous_outcome(observation)
        context = self.context_builder.build(
            observation,
            candidates,
            allowed,
            recent_history=self.trace.recent_actions(
                5, action_generation=_ACTION_GENERATION_CONTEXT.get()
            ),
            previous_outcome=previous_outcome,
            current_goal=purpose,
        )
        if before_provider is not None:
            before_provider()
        if self._uses_context_api():
            raw_decision = self.provider.choose_context(context, personality)
        else:
            raw_decision = self.provider.choose(
                allowed, observation, purpose, personality
            )
            raw_decision = self._bind_legacy_context_references(raw_decision, context)
        reliability = self.decision_verifier.verify(
            raw_decision,
            context,
            observation,
            evaluator_allowed_action_ids=[candidate.action_id for candidate in allowed],
        )
        self.last_reliability_result = reliability
        if reliability.status == ReliabilityStatus.REJECT:
            if commit_guard is None:
                self.trace.append_rejection(context, reliability.to_dict())
            else:
                commit_guard(
                    lambda: self.trace.append_rejection(context, reliability.to_dict())
                )
            failed_checks = ", ".join(
                entry.check
                for entry in reliability.evidence
                if entry.severity == "reject"
            )
            raise ValueError(
                f"Decision rejected by reliability verifier: {failed_checks}"
            )
        decision = self.decision_verifier.normalize_decision(raw_decision)
        if decision is None:
            if commit_guard is None:
                self.trace.append_rejection(context, reliability.to_dict())
            else:
                commit_guard(
                    lambda: self.trace.append_rejection(context, reliability.to_dict())
                )
            raise ValueError(
                "Decision output could not be normalized after verification"
            )
        trace_reference = (context.snapshot_id, decision.action_id)
        staged_writes: list[
            StagedJsonWrite | StagedJournalAppend | DeferredTraceAppend
        ] = []
        try:
            staged_writes.append(self.history.prepare_append(observation, decision))
            staged_writes.append(
                self.trace.prepare_append(
                    context,
                    decision,
                    reliability=reliability.to_dict(),
                    action_generation=_ACTION_GENERATION_CONTEXT.get(),
                )
            )

            # {
            #   責務: [publish_commit: 準備済み履歴と直近判断の状態を一度に公開する]
            #   処理: [historyとplanned traceを保存し、実行前停止用の判断参照だけを設定する]
            #   引数: []
            #   戻り値: []
            # }
            def publish_commit() -> None:
                for staged_write in staged_writes:
                    staged_write.publish()
                self._last_decision_trace_reference = trace_reference

            if commit_guard is None:
                publish_commit()
            else:
                commit_guard(publish_commit)
        finally:
            for staged_write in staged_writes:
                staged_write.discard()
        return decision

    # {
    #   責務: [mark_last_decision_not_executed: 入力を送らなかったplanned判断を監査上の停止として記録する]
    #   処理: [直近判断だけを停止扱いにし、以前の実操作pendingと観測済みOutcomeは変更しない]
    #   引数: [reason: 入力前の停止理由]
    #   戻り値: [なし]
    #   エラー: [RuntimeError: 対象traceが無い、または実送信済みで停止扱いにできない]
    # }
    def mark_last_decision_not_executed(self, reason: str) -> None:
        trace_reference = self._last_decision_trace_reference
        if trace_reference is None:
            raise RuntimeError("実行前停止を記録する判断traceがありません")
        snapshot_id, action_id = trace_reference
        if not self.trace.mark_action_not_executed(snapshot_id, action_id, reason):
            raise RuntimeError("実行前停止を記録する判断traceを更新できません")
        self._last_decision_trace_reference = None

    # {
    #   責務: [
    #     _bind_legacy_context_references: 旧形式のchoose呼び出しが画面参照を返さない場合に限り、現在の判断参照を補う
    #   ]
    #   処理: [
    #     1: ActionDecision以外の値は形を変えずに返す
    #     2: 画面観測ID・画面ID・状態照合値のうち未設定の項目だけ、現在の判断材料から埋める
    #     3: 既に明示された参照、判断元、候補、理由は書き換えない
    #   ]
    #   引数: [
    #     decision_output: 従来のchoose APIが返した値。形式検証前なので任意の型を取り得る
    #     context: 同じ呼び出しで判断元へ渡した画面IDと状態照合値を持つ判断材料
    #   ]
    #   戻り値: [
    #     object: 不足参照だけを補ったActionDecision、または入力と同じ値
    #   ]
    # }
    @staticmethod
    def _bind_legacy_context_references(
        decision_output: object, context: DecisionContext
    ) -> object:
        if not isinstance(decision_output, ActionDecision):
            return decision_output
        missing_references = {
            "snapshot_id": context.snapshot_id,
            "screen_id": str(context.state.get("screen_id", "")),
            "state_signature": str(context.state.get("signature", "")),
        }
        values_to_bind = {
            field_name: reference
            for field_name, reference in missing_references.items()
            if getattr(decision_output, field_name) is None
        }
        return (
            replace(decision_output, **values_to_bind)
            if values_to_bind
            else decision_output
        )

    # {
    #   責務: [step_with_candidate: 従来のstep拡張点を呼び、判断IDに対応する評価済み候補を返す]
    #   処理: [providerへ渡す前に候補評価結果を固定し、停止guardを設定してからself.stepをdispatchする]
    #   引数: [observation: 判断対象の画面観測, candidates: 評価する操作候補, purpose: providerへ渡すゲーム目標, personality: providerへ渡す判断方針, before_provider: provider呼出し前に実行世代を検証する関数, commit_guard: 履歴公開を有効な実行世代内に限定する関数, action_generation: planned判断を結び付ける任意の実行世代]
    #   戻り値: [decision: 拡張stepが返した判断, selected_candidate: 同じ評価snapshotから選んだ候補の実体]
    #   エラー: [判断IDが許可snapshotにない場合はValueError、停止・再開後はExecutionCancelled]
    # }
    def step_with_candidate(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
        before_provider: Callable[[], None] | None = None,
        commit_guard: Callable[[Callable[[], None]], None] | None = None,
        action_generation: int | None = None,
    ) -> tuple[ActionDecision, ActionCandidate]:
        self.last_reliability_result = None
        allowed_snapshot = tuple(self.evaluator.evaluate(observation, candidates))
        allowed_token = _ALLOWED_CANDIDATES_CONTEXT.set(allowed_snapshot)
        provider_token = _BEFORE_PROVIDER_CONTEXT.set(before_provider)
        commit_token = _COMMIT_GUARD_CONTEXT.set(commit_guard)
        generation_token = _ACTION_GENERATION_CONTEXT.set(action_generation)
        try:
            if before_provider is not None:
                before_provider()
            decision = self.step(observation, candidates, purpose, personality)
            if before_provider is not None:
                before_provider()
            selected_candidate = next(
                (
                    candidate
                    for candidate in allowed_snapshot
                    if candidate.action_id == decision.action_id
                ),
                None,
            )
            if selected_candidate is None:
                raise ValueError(
                    "Decision provider selected an action outside the allowed snapshot"
                )
            return decision, selected_candidate
        finally:
            _ACTION_GENERATION_CONTEXT.reset(generation_token)
            _COMMIT_GUARD_CONTEXT.reset(commit_token)
            _BEFORE_PROVIDER_CONTEXT.reset(provider_token)
            _ALLOWED_CANDIDATES_CONTEXT.reset(allowed_token)

    # {
    #   責務: [_uses_context_api: providerがContext APIを実装している場合だけ同APIを選ぶ]
    #   処理: [RuleProvider subclassが旧chooseだけをoverrideした場合は、旧呼出しを優先して拡張互換を保つ]
    #   引数: []
    #   戻り値: [bool: choose_contextを今回の判断呼出しで使う場合True]
    # }
    def _uses_context_api(self) -> bool:
        if not hasattr(self.provider, "choose_context"):
            return False
        provider_type = type(self.provider)
        if (
            isinstance(self.provider, RuleProvider)
            and provider_type is not RuleProvider
        ):
            legacy_choose = getattr(provider_type, "choose", None)
            inherited_context = (
                getattr(provider_type, "choose_context", None)
                is RuleProvider.choose_context
            )
            if legacy_choose is not RuleProvider.choose and inherited_context:
                return False
        return True

    # {
    #   責務: [_semantic_outcome_provider: providerが固有のOutcome判定を持つ場合だけ検出器へ渡す]
    #   処理: [RuleProvider標準実装を除き、assess_outcomeを明示overrideしたproviderを返す]
    #   引数: []
    #   戻り値: [object | None: 固有Outcome判定を持つprovider。該当なしならNone]
    # }
    def _semantic_outcome_provider(self):
        if not hasattr(self.provider, "assess_outcome"):
            return None
        if not isinstance(self.provider, RuleProvider):
            return self.provider
        provider_type = type(self.provider)
        custom_assess = getattr(provider_type, "assess_outcome", None)
        return (
            self.provider if custom_assess is not RuleProvider.assess_outcome else None
        )

    # {
    #   責務: [_assess_previous_outcome: 明示観測で公開済みの同世代実操作結果だけを判断材料へ渡す]
    #   処理: [step内で検出やtrace更新を行わず、未観測・旧世代・実操作なしはunknownを返す]
    #   引数: [observation: 現在の判断画面。新しいOutcomeの根拠として勝手に消費しない]
    #   戻り値: [OutcomeAssessment: 直前の観測済み実操作結果、またはunknown]
    # }
    def _assess_previous_outcome(
        self, observation: ScreenObservation
    ) -> OutcomeAssessment:
        generation = _ACTION_GENERATION_CONTEXT.get()
        receipt = self._pending_execution_receipt
        if (
            not self._pending_outcome_observed
            or self.last_outcome_event is None
            or (
                generation is not None
                and (receipt is None or receipt.action_generation != generation)
            )
        ):
            return OutcomeAssessment(
                "unknown", 0.0, "no completed actual execution observation"
            )
        return self.last_outcome_event.to_assessment()

    # {
    #   責務: [record_execution_receipt: 判断とは独立した実行受領記録だけをpending操作へ進める]
    #   処理: [traceの元snapshotを照合し、現在世代の送信済みまたは結果不明の操作だけを次観測対象にする]
    #   引数: [observation: 入力前の画面, receipt: Executor由来の受領記録,
    #     activate: Falseなら旧世代のauditだけを保存し、pendingや学習状態に反映しない]
    #   戻り値: [なし]
    #   エラー: [ValueError: 画面参照が一致しない, RuntimeError: 元のplanned traceが無い]
    # }
    def record_execution_receipt(
        self,
        observation: ScreenObservation,
        receipt: ExecutionReceipt,
        *,
        activate: bool = True,
    ) -> None:
        if receipt.screen_id != observation.screen_id or receipt.state_signature != str(
            observation.features.get("signature", "")
        ):
            raise ValueError("execution receipt does not match its observation")
        if not activate and receipt.eligible_for_learning:
            receipt = replace(receipt, eligible_for_learning=False)
        if not self.trace.record_execution_receipt(receipt):
            raise RuntimeError("execution receipt has no matching planned trace")
        if self._pending_execution_receipt == receipt:
            return
        if activate and (
            receipt.execution_status == "dispatch_unknown"
            or (
                receipt.execution_status == "executed" and receipt.eligible_for_learning
            )
        ):
            self._pending_execution_receipt = receipt
            self._pending_outcome_observed = False
            self._previous_observation = observation
            self._previous_action_id = receipt.action_id
            self._previous_action_trace_reference = (
                receipt.snapshot_id,
                receipt.action_id,
            )
            self.last_outcome_event = None
            self.last_execution_observation = None

    # {
    #   責務: [complete_execution_observation: 実操作後の明示観測を同じ世代の元操作へ一度だけ結び付ける]
    #   処理: [検出はlock外で行い、公開時にguardとpending同一性を再確認する。terminalでも次判断を要求しない]
    #   引数: [observation: 入力後に取得した画面, action_generation: 観測対象の実行世代,
    #     commit_guard: 現世代の間だけtrace公開を許可する境界]
    #   戻り値: [ExecutionObservationまたはNone: 元receiptと公開済み結果。対象実操作が無ければNone]
    #   エラー: [RuntimeError: 元の実行traceを更新できない, ExecutionCancelled: 世代guardが拒否]
    # }
    def complete_execution_observation(
        self,
        observation: ScreenObservation,
        action_generation: int,
        *,
        commit_guard: Callable[[Callable[[], None]], None] | None = None,
    ) -> ExecutionObservation | None:
        receipt = self._pending_execution_receipt
        before = self._previous_observation
        if (
            receipt is None
            or before is None
            or receipt.action_generation != action_generation
        ):
            return None
        if self._pending_outcome_observed:
            cached = self.last_execution_observation
            if commit_guard is None:
                return cached
            completed: ExecutionObservation | None = None

            # {
            #   責務: [reuse_completed: 同じpendingの観測済み結果だけを現世代で再取得する]
            #   処理: [guard内でpending同一性と観測済み状態を照合して固定参照を返す]
            #   引数: [なし]
            #   戻り値: [なし。completedへ同一実行の観測結果を設定する]
            # }
            def reuse_completed() -> None:
                nonlocal completed
                if (
                    self._pending_execution_receipt == receipt
                    and self._pending_outcome_observed
                ):
                    completed = cached

            commit_guard(reuse_completed)
            return completed
        if receipt.execution_status == "dispatch_unknown":
            event = OutcomeEvent(
                receipt.action_id,
                "unknown",
                0.0,
                False,
                True,
                (),
                "input dispatch outcome is unknown",
                state_changed=False,
            )
        else:
            event = self.outcome_detector.detect(before, receipt.action_id, observation)

        published: ExecutionObservation | None = None

        # {
        #   責務: [publish: 現世代で同じpending実行が残る場合だけ観測結果を公開する]
        #   処理: [trace公開後に観測済みフラグとreceipt/Outcomeペアを保存する]
        #   引数: [なし]
        #   戻り値: [なし]
        #   エラー: [RuntimeError: 元traceを更新できない]
        # }
        def publish() -> None:
            nonlocal published
            if (
                self._pending_execution_receipt != receipt
                or self._pending_outcome_observed
            ):
                return
            outcome = event.to_dict()
            outcome.update(
                {
                    "execution_status": receipt.execution_status,
                    "action_generation": receipt.action_generation,
                    "snapshot_id": receipt.snapshot_id,
                    "eligible_for_learning": receipt.eligible_for_learning,
                }
            )
            if not self.trace.record_action_outcome(
                receipt.snapshot_id,
                receipt.action_id,
                outcome,
                action_generation=receipt.action_generation,
            ):
                raise RuntimeError("actual execution trace is unavailable for outcome")
            self.last_outcome_event = event
            self.last_execution_observation = ExecutionObservation(
                receipt,
                event,
                observation.screen_id,
                str(observation.features.get("signature", "")),
            )
            published = self.last_execution_observation
            self._pending_outcome_observed = True

        if commit_guard is None:
            publish()
        else:
            commit_guard(publish)
        return published
