from dataclasses import dataclass
from ai_game_player.models import ScreenObservation


@dataclass(frozen=True)
class OutcomeAssessment:
    status: str
    confidence: float
    reason: str


class OutcomeEvaluator:
    def __init__(self, success_terms: tuple[str, ...] = ("SUCCESS", "CLEAR", "VICTORY", "COMPLETE"), failure_terms: tuple[str, ...] = ("GAME OVER", "FAILED", "DEFEAT", "失敗")) -> None:
        self.success_terms = success_terms
        self.failure_terms = failure_terms

    def assess(self, observation: ScreenObservation) -> OutcomeAssessment:
        text = " ".join(observation.ocr_text).upper()
        if any(term.upper() in text for term in self.failure_terms):
            return OutcomeAssessment("failure", 0.9, "failure keyword detected")
        if any(term.upper() in text for term in self.success_terms):
            return OutcomeAssessment("success", 0.9, "success keyword detected")
        return OutcomeAssessment("ongoing", 0.4, "no terminal keyword detected")