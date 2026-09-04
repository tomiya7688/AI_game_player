import json
from pathlib import Path
from ai_game_player.action_executor import ExecutionResult
class ExecutionHistory:
    def __init__(self,path:Path): self.path=path
    def append(self,result:ExecutionResult)->None:
        entries=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries,list): raise ValueError("execution_history.json must contain an array")
        entries.append(result.__dict__.copy()); self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_text(json.dumps(entries,ensure_ascii=False,indent=2),encoding="utf-8")