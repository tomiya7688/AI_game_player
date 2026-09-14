from pathlib import Path

from ai_game_player.decision_context import DecisionContextBuilder, DecisionTraceStore
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.history import HistoryStore
from ai_game_player.knowledge import KnowledgeStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment, OutcomeEvaluator
from ai_game_player.provider import RuleProvider


class GamePlayerEngine:
    def __init__(self, game_directory: Path, provider=None, context_builder: DecisionContextBuilder | None = None) -> None:
        self.evaluator = ActionEvaluator()
        self.provider = provider or RuleProvider()
        self.history = HistoryStore(game_directory / "history.json")
        self.trace = DecisionTraceStore(game_directory / "decision_trace.json")
        self.context_builder = context_builder or DecisionContextBuilder(KnowledgeStore(game_directory / "knowledge.json"))
        self._previous_observation: ScreenObservation | None = None

    def step(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
        purpose: str = "",
        personality: str = "",
    ) -> ActionDecision:
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
        if decision.action_id not in {candidate.action_id for candidate in allowed}:
            raise ValueError("Decision provider selected an action outside the allowed snapshot")
        self.history.append(observation, decision)
        self.trace.append(context, decision)
        self._previous_observation = observation
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

    def _assess_previous_outcome(self, observation: ScreenObservation) -> OutcomeAssessment:
        if self._previous_observation is None:
            return OutcomeAssessment("unknown", 0.0, "no previous action")
        if hasattr(self.provider, "assess_outcome"):
            return self.provider.assess_outcome(observation, self._previous_observation)
        return OutcomeEvaluator().assess(observation)
