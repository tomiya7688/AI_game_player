from pathlib import Path
from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.models import ActionCandidate, ActionDecision
from ai_game_player.observation_source import ObservationSource
from ai_game_player.ocr_detector import OcrTextCandidateDetector

class DecisionPipeline:
    def __init__(self, source: ObservationSource, game_directory: Path, provider=None) -> None:
        self.source=source; self.ocr=OcrTextCandidateDetector(); self.merger=CandidateMerger(); self.engine=GamePlayerEngine(game_directory,provider)
    def run(self, ocr_texts: list[dict[str,object]]|None=None, purpose: str="", personality: str="")->ActionDecision:
        observation, configured=self.source.read()
        detected=self.ocr.detect(observation,ocr_texts or [])
        candidates=self.merger.merge(configured,detected)
        return self.engine.step(observation,candidates,purpose,personality)