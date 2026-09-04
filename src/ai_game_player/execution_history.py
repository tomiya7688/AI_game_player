import json
from pathlib import Path
from ai_game_player.action_executor import ExecutionResult
class ExecutionHistory:
    def __init__(self,path:Path): self.path=path
    def load(self)->list[ExecutionResult]:
        if not self.path.exists(): return []
        value=json.loads(self.path.read_text(encoding="utf-8")); return [ExecutionResult(str(x["action_id"]),bool(x["executed"]),str(x["mode"]),str(x.get("detail",""))) for x in value]
    def append(self,result:ExecutionResult)->None:
        entries=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries,list): raise ValueError("execution_history.json must contain an array")
        entries.append(result.__dict__.copy()); self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_text(json.dumps(entries,ensure_ascii=False,indent=2),encoding="utf-8")