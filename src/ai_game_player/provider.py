from ai_game_player.models import ActionCandidate, ActionDecision
class RuleProvider:
    def choose(self, candidates: list[ActionCandidate]) -> ActionDecision:
        if not candidates: raise ValueError("許可された操作候補がありません")
        c=max(candidates,key=lambda x:x.confidence)
        return ActionDecision(c.action_id,"信頼度が最も高い安全な候補","local_rule")
