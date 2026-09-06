from collections import deque
from ai_game_player.models import ScreenObservation


class LoopGuard:
    def __init__(self, limit: int = 3) -> None:
        if limit < 2:
            raise ValueError("loop limit must be at least 2")
        self.limit = limit
        self._recent: deque[tuple[str, int, int, tuple[str, ...]]] = deque(maxlen=limit)

    def observe(self, observation: ScreenObservation) -> bool:
        signature = (observation.screen_id, observation.width, observation.height, tuple(observation.ocr_text))
        self._recent.append(signature)
        return len(self._recent) == self.limit and len(set(self._recent)) == 1

    def reset(self) -> None:
        self._recent.clear()