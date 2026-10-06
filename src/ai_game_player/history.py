import json
from pathlib import Path

from ai_game_player.atomic_json import write_json_array_atomically
from ai_game_player.models import ActionDecision, ScreenObservation
class HistoryStore:
    def __init__(self,path:Path): self.path=path
    def append(self,observation:ScreenObservation,decision:ActionDecision)->None:
        entries=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries,list): raise ValueError("history.json must contain an array")
        entries.append({"observation":observation.to_dict(),"decision":decision.to_dict()})
        write_json_array_atomically(self.path, entries)
