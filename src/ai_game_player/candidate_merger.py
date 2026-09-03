from ai_game_player.models import ActionCandidate

class CandidateMerger:
    def __init__(self, proximity: int = 20) -> None: self.proximity=proximity
    def merge(self, automation: list[ActionCandidate], ocr: list[ActionCandidate]) -> list[ActionCandidate]:
        result=list(automation)
        for candidate in ocr:
            if any(self._same(existing,candidate) for existing in result): continue
            result.append(candidate)
        return result
    def _same(self,left:ActionCandidate,right:ActionCandidate)->bool:
        if left.action_id==right.action_id: return True
        if left.x is None or left.y is None or right.x is None or right.y is None: return False
        return abs(left.x-right.x)<=self.proximity and abs(left.y-right.y)<=self.proximity