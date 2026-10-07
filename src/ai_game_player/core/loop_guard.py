from collections import deque

from ai_game_player.models import ScreenObservation
from ai_game_player.screen_similarity import ScreenSimilarity


class LoopGuard:
    def __init__(self, limit: int = 3, similarity: ScreenSimilarity | None = None) -> None:
        if limit < 2:
            raise ValueError("loop limit must be at least 2")
        self.limit = limit
        self.similarity = similarity or ScreenSimilarity()
        self._recent: deque[ScreenObservation] = deque(maxlen=limit)

    def observe(self, observation: ScreenObservation) -> bool:
        self._recent.append(observation)
        if len(self._recent) != self.limit:
            return False
        reference = self._recent[0]
        return all(
            self.similarity.compare(reference, recent)["category"] in {"identical", "nearly_same"}
            for recent in self._recent
        )

    def reset(self) -> None:
        self._recent.clear()