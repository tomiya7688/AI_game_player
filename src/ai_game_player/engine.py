from pathlib import Path
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.history import HistoryStore
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.provider import RuleProvider
class GamePlayerEngine:
    def __init__(self,game_directory:Path,provider:RuleProvider|None=None): self.evaluator=ActionEvaluator(); self.provider=provider or RuleProvider(); self.history=HistoryStore(game_directory/"history.json")
    def step(self,observation:ScreenObservation,candidates:list[ActionCandidate],purpose:str="",personality:str="")->ActionDecision:
        decision=self.provider.choose(self.evaluator.evaluate(observation,candidates),observation,purpose,personality); self.history.append(observation,decision); return decision