from collections.abc import Callable
from pathlib import Path

from ai_game_player.atomic_json import StagedJsonWrite
from ai_game_player.decision_context import DecisionContextBuilder, DecisionTraceStore
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.history import HistoryStore
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.outcome_fusion import OutcomeDetector
from ai_game_player.outcome_models import OutcomeEvent
from ai_game_player.provider import RuleProvider


class GamePlayerEngine:
    def __init__(
        self,
        game_directory: Path,
        provider=None,
        context_builder: DecisionContextBuilder | None = None,
        outcome_detector: OutcomeDetector | None = None,
    ) -> None:
        self.evaluator = ActionEvaluator()
        self.provider = provider or RuleProvider()
        self.history = HistoryStore(game_directory / "history.json")
        self.trace = DecisionTraceStore(game_directory / "decision_trace.json")
        self.context_builder = context_builder or DecisionContextBuilder(KnowledgeStore(game_directory / "knowledge.json"))
        self.outcome_detector = outcome_detector or OutcomeDetector(self._semantic_outcome_provider())
        self.last_outcome_event: OutcomeEvent | None = None
        self._previous_observation: ScreenObservation | None = None
        self._previous_action_id: str | None = None

    # {
    #   責務: [step: 許可候補から判断し、停止確認後に判断履歴を確定する]
    #   処理: [provider応答を検証し、履歴JSONを事前準備してから世代guard内で公開する]
    #   引数: [
    #     observation: provider判断と履歴記録に使う画面観測
    #     candidates: evaluatorが検査しproviderが選択できる操作候補
    #     purpose: providerへ渡すゲーム目標
    #     personality: providerへ渡す判断方針
    #     before_provider: provider呼出し前に実行世代を検証する関数
    #     commit_guard: 準備済み履歴とengine状態を世代guard内で公開する関数
    #   ]
    #   戻り値: [ActionDecision: 許可候補から選ばれた判断]
    #   エラー: [ExecutionCancelled: commit前に実行世代が停止または失効した, OSError: 準備JSONの公開に失敗した]
    # }
    def step(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
        before_provider: Callable[[], None] | None = None,
        commit_guard: Callable[[Callable[[], None]], None] | None = None,
    ) -> ActionDecision:
        if before_provider is not None:
            before_provider()
        allowed = self.evaluator.evaluate(observation, candidates)
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
        if decision.action_id not in {candidate.action_id for candidate in allowed}:
            raise ValueError("Decision provider selected an action outside the allowed snapshot")
        staged_writes: list[StagedJsonWrite] = []
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
