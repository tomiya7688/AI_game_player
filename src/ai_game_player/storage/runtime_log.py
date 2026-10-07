import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class RuntimeLog:
    """Application runtime events written as JSONL."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path("user_data/output/log/ai_game_player.jsonl")

    def write(self, event: str, message: str = "", context: Mapping[str, Any] | None = None) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": str(event), "message": str(message), "context": dict(context or {})}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")