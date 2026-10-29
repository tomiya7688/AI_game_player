from dataclasses import replace
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

from ai_game_player.decision_context import DecisionContext, DecisionContextBuilder, DecisionTraceStore
from ai_game_player.decision_verifier import DecisionVerifier, ReliabilityResult, ReliabilityStatus
from ai_game_player.atomic_json import StagedJsonWrite
from ai_game_player.event_journal import EventJournal
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.history import HistoryStore
from ai_game_player.journal_adapters import LegacyEventAdapter, StagedJournalAppend
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.outcome_fusion import OutcomeDetector
from ai_game_player.outcome_models import OutcomeEvent
from ai_game_player.provider import RuleProvider


_BEFORE_PROVIDER_CONTEXT: ContextVar[Callable[[], None] | None] = ContextVar("before_provider", default=None)
_COMMIT_GUARD_CONTEXT: ContextVar[Callable[[Callable[[], None]], None] | None] = ContextVar("commit_guard", default=None)
_ALLOWED_CANDIDATES_CONTEXT: ContextVar[tuple[ActionCandidate, ...] | None] = ContextVar("allowed_candidates", default=None)


# {
#   責務: [GamePlayerEngine: 許可候補の判断・信頼性検査・Outcome追跡とJournal保存を管理する]
#   フィールド: [decision_verifier: provider回答を画面と候補へ照合する検査器, trace: snapshotに判断根拠と実行結果を結ぶ追記履歴, event_journal: 指定時に履歴を保存するSession Journal]
#   処理: [停止世代guard内で履歴を公開し、実行前に止めた判断を次の行動履歴から除外する]
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
            if event_journal is not None else None,
        )
        self.trace = DecisionTraceStore(
            trace_path,
            LegacyEventAdapter(
                event_journal,
                trace_path,
                "decision.trace",
                migrate_legacy=migrate_legacy,
            )
            if event_journal is not None else None,
        )
        self.context_builder = context_builder or DecisionContextBuilder(KnowledgeStore(game_directory / "knowledge.json"))
        self.outcome_detector = outcome_detector or OutcomeDetector(self._semantic_outcome_provider())
        self.decision_verifier = DecisionVerifier()
        self.last_reliability_result: ReliabilityResult | None = None
        self.last_outcome_event: OutcomeEvent | None = None
        self._previous_observation: ScreenObservation | None = None
        self._previous_action_id: str | None = None
        self._last_decision_trace_reference: tuple[str, str] | None = None
        self._previous_action_trace_reference: tuple[str, str] | None = None
        self._previous_action_state_before_last_decision: tuple[
            ScreenObservation | None,
            str | None,
            tuple[str, str] | None,
        ] | None = None

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
        previous_action_state = (
            self._previous_observation,
            self._previous_action_id,
            self._previous_action_trace_reference,
        )
        self._previous_action_state_before_last_decision = None
        candidate_snapshot = _ALLOWED_CANDIDATES_CONTEXT.get()
        allowed_snapshot = tuple(candidate_snapshot) if candidate_snapshot is not None else tuple(self.evaluator.evaluate(observation, candidates))
        allowed = list(allowed_snapshot)
        previous_outcome = self._assess_previous_outcome(observation)
        context = self.context_builder.build(
            observation,
            candidates,
            allowed,
            recent_history=self.trace.recent_actions(5),
            previous_outcome=previous_outcome,
            current_goal=purpose,
        )
        if before_provider is not None:
            before_provider()
        if self._uses_context_api():
            raw_decision = self.provider.choose_context(context, personality)
        else:
            raw_decision = self.provider.choose(allowed, observation, purpose, personality)
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
                commit_guard(lambda: self.trace.append_rejection(context, reliability.to_dict()))
            failed_checks = ", ".join(
                entry.check for entry in reliability.evidence if entry.severity == "reject"
            )
            raise ValueError(f"Decision rejected by reliability verifier: {failed_checks}")
        decision = self.decision_verifier.normalize_decision(raw_decision)
        if decision is None:
            if commit_guard is None:
                self.trace.append_rejection(context, reliability.to_dict())
            else:
                commit_guard(lambda: self.trace.append_rejection(context, reliability.to_dict()))
            raise ValueError("Decision output could not be normalized after verification")
        trace_reference = (context.snapshot_id, decision.action_id)
        staged_writes: list[StagedJsonWrite | StagedJournalAppend] = []
        try:
            staged_writes.append(self.history.prepare_append(observation, decision))
            staged_writes.append(
                self.trace.prepare_append(context, decision, reliability=reliability.to_dict())
            )

            # {
            #   責務: [publish_commit: 準備済み履歴と直近判断の状態を一度に公開する]
            #   処理: [historyとtraceを保存し、次回Outcome判定および実行前停止用の参照を設定する]
            #   引数: []
            #   戻り値: []
            # }
            def publish_commit() -> None:
                for staged_write in staged_writes:
                    staged_write.publish()
                self._previous_action_state_before_last_decision = previous_action_state
                self._previous_observation = observation
                self._previous_action_id = decision.action_id
                self._last_decision_trace_reference = trace_reference
                self._previous_action_trace_reference = trace_reference

            if commit_guard is None:
                publish_commit()
            else:
                commit_guard(publish_commit)
        finally:
            for staged_write in staged_writes:
                staged_write.discard()
        return decision

    # {
    #   責務: [
    #     mark_last_decision_not_executed: 安全確認などで実行しなかった直近判断を履歴から除き、以前の操作状態へ戻す
    #   ]
    #   処理: [
    #     1: 直近判断の画面IDと操作IDに停止理由を保存し、行動履歴から除外する
    #     2: 今回の判断を作る前に退避した、直前に実行済みの操作状態を復元する
    #     3: 次の画面観測を直前の実行済み操作の結果として比較できるよう参照を戻す
    #   ]
    #   引数: [
    #     reason: 操作を送らなかった理由。安全判定や実行条件の監査履歴へ保存する
    #   ]
    #   戻り値: [なし。直近の判断を停止済みとして記録し、以前の操作状態へ戻す]
    #   エラー: [
    #     RuntimeError: 直近判断または退避状態がなく、対応する記録を更新できない場合
    #   ]
    # }
    def mark_last_decision_not_executed(self, reason: str) -> None:
        trace_reference = self._last_decision_trace_reference
        if trace_reference is None:
            raise RuntimeError("実行前停止を記録する判断traceがありません")
        previous_action_state = self._previous_action_state_before_last_decision
        if previous_action_state is None:
            raise RuntimeError("実行前停止の前に保持した操作状態がありません")
        snapshot_id, action_id = trace_reference
        if not self.trace.mark_action_not_executed(snapshot_id, action_id, reason):
            raise RuntimeError("実行前停止を記録する判断traceを更新できません")
        self._last_decision_trace_reference = None
        self._previous_action_state_before_last_decision = None
        (
            self._previous_observation,
            self._previous_action_id,
            self._previous_action_trace_reference,
        ) = previous_action_state

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
    def _bind_legacy_context_references(decision_output: object, context: DecisionContext) -> object:
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
        return replace(decision_output, **values_to_bind) if values_to_bind else decision_output

    # {
    #   責務: [step_with_candidate: 従来のstep拡張点を呼び、判断IDに対応する評価済み候補を返す]
    #   処理: [providerへ渡す前に候補評価結果を固定し、停止guardを設定してからself.stepをdispatchする]
    #   引数: [observation: 判断対象の画面観測, candidates: 評価する操作候補, purpose: providerへ渡すゲーム目標, personality: providerへ渡す判断方針, before_provider: provider呼出し前に実行世代を検証する関数, commit_guard: 履歴公開を有効な実行世代内に限定する関数]
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
    ) -> tuple[ActionDecision, ActionCandidate]:
        self.last_reliability_result = None
        allowed_snapshot = tuple(self.evaluator.evaluate(observation, candidates))
        allowed_token = _ALLOWED_CANDIDATES_CONTEXT.set(allowed_snapshot)
        provider_token = _BEFORE_PROVIDER_CONTEXT.set(before_provider)
        commit_token = _COMMIT_GUARD_CONTEXT.set(commit_guard)
        try:
            if before_provider is not None:
                before_provider()
            decision = self.step(observation, candidates, purpose, personality)
            if before_provider is not None:
                before_provider()
            selected_candidate = next(
                (candidate for candidate in allowed_snapshot if candidate.action_id == decision.action_id),
                None,
            )
            if selected_candidate is None:
                raise ValueError("Decision provider selected an action outside the allowed snapshot")
            return decision, selected_candidate
        finally:
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
        if isinstance(self.provider, RuleProvider) and provider_type is not RuleProvider:
            legacy_choose = getattr(provider_type, "choose", None)
            inherited_context = getattr(provider_type, "choose_context", None) is RuleProvider.choose_context
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
        return self.provider if custom_assess is not RuleProvider.assess_outcome else None

    # {
    #   責務: [_assess_previous_outcome: 前回実行判断と現在画面を比較してOutcomeを評価する]
    #   処理: [検出結果をtraceの元判断へ結び付け、次のDecision Context向けOutcomeAssessmentへ変換する]
    #   引数: [observation: 直前判断の後で取得した現在画面]
    #   戻り値: [OutcomeAssessment: 前回操作の進行・成功・失敗・不確実性]
    # }
    def _assess_previous_outcome(self, observation: ScreenObservation) -> OutcomeAssessment:
        if self._previous_observation is None or self._previous_action_id is None:
            self.last_outcome_event = None
            return OutcomeAssessment("unknown", 0.0, "no previous action")
        event = self.outcome_detector.detect(
            self._previous_observation,
            self._previous_action_id,
            observation,
        )
        self.last_outcome_event = event
        if self._previous_action_trace_reference is not None:
            outcome = event.to_dict()
            self.trace.record_action_outcome(*self._previous_action_trace_reference, outcome)
        return event.to_assessment()
