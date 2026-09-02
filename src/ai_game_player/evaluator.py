from ai_game_player.models import ActionCandidate, ScreenObservation
class ActionEvaluator:
    SUPPORTED=frozenset({"click","double_click","key","wait"})
    def evaluate(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> list[ActionCandidate]:
        result=[]; seen=set()
        for c in candidates:
            if c.action_id in seen or c.kind not in self.SUPPORTED or c.dangerous or c.confidence<.5: continue
            seen.add(c.action_id)
            if c.kind in {"click","double_click"} and (c.x is None or c.y is None or not (0<=c.x<observation.width and 0<=c.y<observation.height)): continue
            result.append(c)
        return result
