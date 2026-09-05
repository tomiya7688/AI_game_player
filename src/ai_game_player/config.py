import json
from dataclasses import asdict, dataclass
from pathlib import Path

@dataclass
class AppConfig:
    provider: str = "ローカル規則"
    model: str = "gemma3:4b"
    endpoint: str = "http://127.0.0.1:11434"
    personality: str = "好奇心旺盛"
    purpose: str = "画面の役割を理解する"
    live_execution: bool = False
    input_mode: str = "window_message"

class ConfigStore:
    def __init__(self,path:Path): self.path=path
    def load(self)->AppConfig:
        if not self.path.exists(): return AppConfig()
        value=json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value,dict): raise ValueError("config.json must contain an object")
        defaults=asdict(AppConfig()); defaults.update({key:value[key] for key in defaults if key in value})
        defaults["live_execution"] = bool(defaults["live_execution"])
        if defaults["input_mode"] not in {"window_message", "mouse"}: defaults["input_mode"] = "window_message"
        return AppConfig(**defaults)
    def save(self,config:AppConfig)->None:
        self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_text(json.dumps(asdict(config),ensure_ascii=False,indent=2),encoding="utf-8")