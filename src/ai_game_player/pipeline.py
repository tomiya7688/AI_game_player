from pathlib import Path
from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.models import ActionCandidate, ActionDecision
from ai_game_player.observation_source import ObservationSource
from ai_game_player.ocr_detector import OcrTextCandidateDetector
from ai_game_player.action_executor import ActionExecutor, ExecutionResult
from ai_game_player.execution_history import ExecutionHistory

class DecisionPipeline:
    def __init__(self, source: ObservationSource, game_directory: Path, provider=None) -> None:
        self.source=source; self.ocr=OcrTextCandidateDetector(); self.merger=CandidateMerger(); self.engine=GamePlayerEngine(game_directory,provider); self.executor=ActionExecutor(); self.execution_history=ExecutionHistory(game_directory/"execution_history.json")
    def run(self, ocr_texts: list[dict[str,object]]|None=None, purpose: str="", personality: str="")->ActionDecision:
        observation, configured=self.source.read()
        detected=self.ocr.detect(observation,ocr_texts or [])
        candidates=self.merger.merge(configured,detected)
        return self.engine.step(observation,candidates,purpose,personality)
    def run_and_execute(self, ocr_texts=None, purpose: str="", personality: str="")->ExecutionResult:
        decision=self.run(ocr_texts,purpose,personality); _, candidates=self.source.read(); selected=next((c for c in candidates if c.action_id==decision.action_id),None)
        if selected is None: raise RuntimeError("決定された候補が入力Sourceにありません")
        result=self.executor.execute(selected); self.execution_history.append(result); return result