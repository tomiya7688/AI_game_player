from typing import Protocol
from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.screen_capture import ScreenFrame

class FrameCapture(Protocol):
    def capture(self)->ScreenFrame: ...

class CapturedObservationSource:
    def __init__(self,capture:FrameCapture,screen_id:str="screen") -> None: self.capture_device=capture; self.analyzer=FrameAnalyzer(); self.screen_id=screen_id
    def read(self)->tuple[ScreenObservation,list[ActionCandidate]]:
        return self.analyzer.analyze(self.capture_device.capture(),self.screen_id),[]