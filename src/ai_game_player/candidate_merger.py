from ai_game_player.models import ActionCandidate


class CandidateMerger:
    def __init__(self, proximity: int = 20) -> None:
        self.proximity = proximity

    def merge(
        self,
        automation: list[ActionCandidate],
        ocr: list[ActionCandidate],
        image: list[ActionCandidate] | None = None,
    ) -> list[ActionCandidate]:
        result = list(automation)
        for candidates in (ocr, image or []):
            for candidate in candidates:
                if not any(self._same(existing, candidate) for existing in result):
                    result.append(candidate)
        return result

    def _same(self, left: ActionCandidate, right: ActionCandidate) -> bool:
        if left.action_id == right.action_id:
            return True
        if left.x is None or left.y is None or right.x is None or right.y is None:
            return False
        return abs(left.x - right.x) <= self.proximity and abs(left.y - right.y) <= self.proximity