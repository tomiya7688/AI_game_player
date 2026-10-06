from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.atomic_json import write_json_array_atomically


# {
#   責務: [ExecutionHistory: 実行結果をJSON配列として永続化する]
#   フィールド: [path: 実行履歴ファイルの保存先]
#   処理: [実行結果の読込と追記を提供する]
# }
class ExecutionHistory:
    """Persist execution results as a JSON array with atomic replacement."""

    # {
    #   責務: [__init__: 履歴ファイルの保存先を設定する]
    #   処理: [pathをインスタンスに保持する]
    #   引数: [path: 履歴JSONファイルのパス]
    #   戻り値: []
    # }
    def __init__(self, path: Path) -> None:
        self.path = path

    # {
    #   責務: [load: 永続化済み実行結果を型付き値として読み込む]
    #   処理: [_read_entriesの各要素をExecutionResultへ変換する]
    #   引数: []
    #   戻り値: [list[ExecutionResult]: 保存済みの実行結果]
    # }
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

    # {
    #   責務: [append: 実行結果を履歴へ原子的に追記する]
    #   処理: [既存配列へresultを追加し、同一ディレクトリの一時ファイル経由で置換する]
    #   引数: [result: 追記する実行結果]
    #   戻り値: []
    # }
    def append(self, result: ExecutionResult) -> None:
        entries = self._read_entries()
        entries.append(asdict(result))
        write_json_array_atomically(self.path, entries)

    # {
    #   責務: [_read_entries: 保存済みJSON配列を検証して読み込む]
    #   処理: [未作成なら空配列を返し、JSON構文と配列要素の型を検証する]
    #   引数: []
    #   戻り値: [list[dict[str, object]]: 検証済みの履歴項目]
    #   エラー: [不正JSONまたは配列以外・object以外の要素でValueError]
    # }
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
