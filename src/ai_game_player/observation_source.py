import json
from pathlib import Path
from typing import Protocol
from ai_game_player.models import ActionCandidate, ScreenObservation

class ObservationSource(Protocol):
    def read(self) -> tuple[ScreenObservation,list[ActionCandidate]]: ...

class JsonObservationSource:
    def __init__(self,path:Path): self.path=path
    def read(self)->tuple[ScreenObservation,list[ActionCandidate]]:
        value=json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value,dict): raise ValueError("observation JSON must be an object")
        observation=ScreenObservation(screen_id=str(value["screen_id"]),width=int(value["width"]),height=int(value["height"]),ocr_text=[str(x) for x in value.get("ocr_text",[])],features=dict(value.get("features",{})))
        raw=value.get("candidates",[])
        if not isinstance(raw,list): raise ValueError("candidates must be an array")
        return observation,[ActionCandidate.from_dict(x) for x in raw]