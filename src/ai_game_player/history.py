import json
from pathlib import Path
from uuid import uuid4
from ai_game_player.models import ActionDecision, ScreenObservation
class HistoryStore:
    def __init__(self,path:Path): self.path=path
    def append(self,observation:ScreenObservation,decision:ActionDecision)->None:
        entries=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries,list): raise ValueError("history.json must contain an array")
        entries.append({"observation":observation.to_dict(),"decision":decision.to_dict()})
        self.path.parent.mkdir(parents=True,exist_ok=True); temp=self.path.with_suffix(f".{uuid4().hex}.tmp")
        temp.write_text(json.dumps(entries,ensure_ascii=False,indent=2),encoding="utf-8"); temp.replace(self.path)
