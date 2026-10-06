from pathlib import Path

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
    #   責務: [
    #     step: 画面観測から操作判断を行い、判断結果を既存API形式で返す
    #   ]
    #   処理: [
    #     1: 同一スナップショットに基づく判断と候補選択を行う
    #     2: 選択判断を履歴へ保存する
    #   ]
    #   引数: [
    #     observation: 判断対象の画面観測
    #     candidates: 評価する操作候補
    #     purpose: 現在の目的
    #     personality: Providerへ渡す判断方針
    #   ]
    #   戻り値: [
    #     decision: Providerが選択した操作判断
    #   ]
    # }
    def step(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
    ) -> ActionDecision:
        decision, _ = self.step_with_candidate(observation, candidates, purpose, personality)
        return decision

    # {
    #   責務: [
    #     step_with_candidate: 判断と同じ評価スナップショットから選択候補を返す
    #   ]
    #   処理: [
    #     1: 候補を評価して許可候補だけをProviderへ渡す
    #     2: 判断IDに対応する許可済み候補オブジェクトを特定する
    #     3: 判断と同じ画面観測・候補の組を履歴へ保存する
    #   ]
    #   引数: [
    #     observation: 判断対象の画面観測
    #     candidates: 評価する操作候補
    #     purpose: 現在の目的
    #     personality: Providerへ渡す判断方針
    #   ]
    #   戻り値: [
    #     decision: Providerが選択した操作判断
    #     selected_candidate: 判断で選ばれた許可済み候補の実体
    #   ]
    #   エラー: [
    #     Providerが許可されていない操作IDを返した場合はValueErrorを送出する
    #   ]
    # }
    def step_with_candidate(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
    ) -> tuple[ActionDecision, ActionCandidate]:
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
        if self._uses_context_api():
            decision = self.provider.choose_context(context, personality)
        else:
            decision = self.provider.choose(allowed, observation, purpose, personality)
        selected_candidate = next(
            (candidate for candidate in allowed if candidate.action_id == decision.action_id),
            None,
        )
        if selected_candidate is None:
            raise ValueError("Decision provider selected an action outside the allowed snapshot")
        self.history.append(observation, decision)
        self.trace.append(context, decision)
        self._previous_observation = observation
        self._previous_action_id = decision.action_id
        return decision, selected_candidate

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
