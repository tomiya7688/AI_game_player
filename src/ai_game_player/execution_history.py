from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.atomic_json import write_json_array_atomically


class ExecutionHistory:
    """Persist execution results as a JSON array with atomic replacement."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[ExecutionResult]:
        return [
            ExecutionResult(
                str(entry["action_id"]),
                bool(entry["executed"]),
                str(entry["mode"]),
                str(entry.get("detail", "")),
            )
            for entry in self._read_entries()
        ]

    def append(self, result: ExecutionResult) -> None:
        entries = self._read_entries()
        entries.append(asdict(result))
        write_json_array_atomically(self.path, entries)

    def _read_entries(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []

        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid execution history JSON in {self.path}: {error}") from error
        if not isinstance(value, list):
            raise ValueError("execution_history.json must contain an array")
        if any(not isinstance(entry, dict) for entry in value):
            raise ValueError("execution_history.json entries must be objects")
        return value
