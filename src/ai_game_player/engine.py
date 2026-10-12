from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

from ai_game_player.atomic_json import StagedJsonWrite
from ai_game_player.decision_context import DecisionContextBuilder, DecisionTraceStore
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
#   責務: [GamePlayerEngine: Observation・候補・判断・Outcomeを接続し、履歴をSession Journalへ保存する]
#   フィールド: [history/trace: 旧reader互換の判断履歴adapter, event_journal: 任意のSession event保存先]
# }
class GamePlayerEngine:
    # {
    #   責務: [__init__: 判断・履歴・Outcome componentをgame directoryと任意Journalへ接続する]
    #   処理: [旧history/trace JSONを指定Journalへ移行し、providerとcontext builderを初期化する]
    #   引数: [game_directory: game固有のlegacy data保存先, provider: 判断provider, context_builder: 判断context生成器, outcome_detector: 直前操作を評価するdetector, event_journal: 現Sessionのevent保存先, migrate_legacy: 初回sessionなら旧historyとtraceをJournalへ移行する]
    #   戻り値: []
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
        self.last_outcome_event: OutcomeEvent | None = None
        self._previous_observation: ScreenObservation | None = None
        self._previous_action_id: str | None = None

    # {
    #   責務: [step: 許可候補から判断し、停止確認後に判断履歴を確定する]
    #   処理: [provider応答を検証し、履歴JSONを事前準備してから世代guard内で公開する]
    #   引数: [observation: 判断対象の画面観測, candidates: 評価する操作候補, purpose: providerへ渡すゲーム目標, personality: providerへ渡す判断方針]
    #   戻り値: [ActionDecision: 許可候補から選ばれた判断]
    #   エラー: [停止または再開で世代が失効した場合はExecutionCancelled、履歴JSONの公開に失敗した場合はOSError]
    # }
    def step(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
    ) -> ActionDecision:
        before_provider = _BEFORE_PROVIDER_CONTEXT.get()
        commit_guard = _COMMIT_GUARD_CONTEXT.get()
        if before_provider is not None:
            before_provider()
        candidate_snapshot = _ALLOWED_CANDIDATES_CONTEXT.get()
        allowed_snapshot = tuple(candidate_snapshot) if candidate_snapshot is not None else tuple(self.evaluator.evaluate(observation, candidates))
        allowed = list(allowed_snapshot)
        previous_outcome = self._assess_previous_outcome(observation)
        context = self.context_builder.build(
            observation,
            candidates,
            allowed,
            recent_history=self.trace.recent(5),
            previous_outcome=previous_outcome,
            current_goal=purpose,
        )
        if before_provider is not None:
            before_provider()
        if self._uses_context_api():
            decision = self.provider.choose_context(context, personality)
        else:
            decision = self.provider.choose(allowed, observation, purpose, personality)
        if not any(candidate.action_id == decision.action_id for candidate in allowed_snapshot):
            raise ValueError("Decision provider selected an action outside the allowed snapshot")
        staged_writes: list[StagedJsonWrite | StagedJournalAppend] = []
        try:
            staged_writes.append(self.history.prepare_append(observation, decision))
            staged_writes.append(self.trace.prepare_append(context, decision))

            # {
            #   責務: [publish_commit: 準備済み履歴とengineの前回判断状態を公開する]
            #   処理: [判断履歴・traceの一時JSONを置換し、同じ判断の観測とaction IDを保持する]
            #   引数: []
            #   戻り値: []
            # }
            def publish_commit() -> None:
                for staged_write in staged_writes:
                    staged_write.publish()
                self._previous_observation = observation
                self._previous_action_id = decision.action_id

            if commit_guard is None:
                publish_commit()
            else:
                commit_guard(publish_commit)
        finally:
            for staged_write in staged_writes:
                staged_write.discard()
        return decision

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

    def _semantic_outcome_provider(self):
        if not hasattr(self.provider, "assess_outcome"):
            return None
        if not isinstance(self.provider, RuleProvider):
            return self.provider
        provider_type = type(self.provider)
        custom_assess = getattr(provider_type, "assess_outcome", None)
        return self.provider if custom_assess is not RuleProvider.assess_outcome else None

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
        return event.to_assessment()
