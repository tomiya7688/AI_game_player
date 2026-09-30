from __future__ import annotations

import random
from time import perf_counter_ns

from ai_game_player.decision_context import DecisionContext
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment


class NoisyLanguageProvider:
    """Deterministic low-quality pseudo-LM for robustness/performance CI.

    This intentionally does not try to make a good decision. It selects an
    allowed action pseudo-randomly and emits semantically useless word salad.
    The seed keeps CI reproducible while still exercising non-greedy choices.
    """

    WORDS = (
        "banana",
        "window",
        "purple",
        "maybe",
        "echo",
        "seven",
        "north",
        "paper",
        "cloud",
        "almost",
        "button",
        "river",
        "triangle",
        "yesterday",
        "quiet",
        "pixel",
        "spoon",
        "orbit",
        "green",
        "because",
        "later",
        "glass",
        "random",
        "chair",
    )
    OUTCOMES = ("unknown", "ongoing", "failure", "success")
    CONFIDENCES = (0.05, 0.1, 0.15, 0.2, 0.25)

    def __init__(self, seed: int = 1729, words_per_reason: int = 12) -> None:
        if words_per_reason < 1:
            raise ValueError("words_per_reason must be positive")
        self.seed = seed
        self.words_per_reason = words_per_reason
        self._random = random.Random(seed)
        self._elapsed_ns = 0
        self.decision_calls = 0
        self.outcome_calls = 0

    def choose(
        self,
        candidates: list[ActionCandidate],
        observation: ScreenObservation | None = None,
        purpose: str = "",
        personality: str = "",
    ) -> ActionDecision:
        del observation, purpose, personality
        allowed_action_ids = [candidate.action_id for candidate in candidates]
        return self._timed_choose(allowed_action_ids)

    def choose_context(self, context: DecisionContext, personality: str = "") -> ActionDecision:
        del personality
        return self._timed_choose(list(context.allowed_action_ids))

    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None = None,
    ) -> OutcomeAssessment:
        del observation, previous
        started = perf_counter_ns()
        try:
            self.outcome_calls += 1
            status = self._random.choice(self.OUTCOMES)
            confidence = self._random.choice(self.CONFIDENCES)
            return OutcomeAssessment(status, confidence, self._word_salad())
        finally:
            self._elapsed_ns += perf_counter_ns() - started

    def consume_elapsed_ms(self) -> float:
        """Return Provider CPU time accumulated since the previous call."""
        elapsed = self._elapsed_ns / 1_000_000.0
        self._elapsed_ns = 0
        return elapsed

    def _timed_choose(self, allowed_action_ids: list[str]) -> ActionDecision:
        started = perf_counter_ns()
        try:
            if not allowed_action_ids:
                raise ValueError("NoisyLanguageProvider requires at least one allowed action")
            self.decision_calls += 1
            action_id = self._random.choice(allowed_action_ids)
            return ActionDecision(action_id, self._word_salad(), "ci:noisy-language-provider")
        finally:
            self._elapsed_ns += perf_counter_ns() - started

    def _word_salad(self) -> str:
        return " ".join(self._random.choice(self.WORDS) for _ in range(self.words_per_reason))


class OutOfSetNoisyLanguageProvider(NoisyLanguageProvider):
    """Negative-test Provider that intentionally violates candidate grounding."""

    def choose_context(self, context: DecisionContext, personality: str = "") -> ActionDecision:
        del context, personality
        started = perf_counter_ns()
        try:
            self.decision_calls += 1
            return ActionDecision(
                "__outside_allowed_snapshot__",
                self._word_salad(),
                "ci:noisy-language-provider:invalid",
            )
        finally:
            self._elapsed_ns += perf_counter_ns() - started
