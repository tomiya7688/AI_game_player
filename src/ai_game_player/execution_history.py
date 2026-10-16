from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.atomic_json import write_json_array_atomically
from ai_game_player.journal_adapters import LegacyEventAdapter


# {
#   責務: [ExecutionHistory: 実行結果を旧JSON互換readerまたはSession Event Journalで管理する]
#   フィールド: [path: 旧JSON履歴の保存先, event_adapter: 設定時に実行結果を保存するSession Event Journal adapter]
#   処理: [実行結果の読込みと、旧JSONまたはSession Journalへの追記を提供する]
# }
class ExecutionHistory:
    # {
    #   責務: [__init__: 旧履歴の保存先と任意のJournal adapterを関連付ける]
    #   処理: [pathと任意のJournal adapterをインスタンスに保持する]
    #   引数: [path: 旧形式のexecution_history.jsonの保存先, event_adapter: 指定時に履歴を保存するSession Journal adapter]
    #   戻り値: []
    # }
    def __init__(
        self,
        path: Path,
        event_adapter: LegacyEventAdapter | None = None,
    ) -> None:
        self.path = path
        self.event_adapter = event_adapter

    # {
    #   責務: [load: 旧形式readerと同じExecutionResult一覧を返す]
    #   処理: [Journal adapterがあればその互換cacheを使い、なければ旧JSON配列をExecutionResultへ変換する]
    #   引数: [self: 実行履歴store]
    #   戻り値: [list[ExecutionResult]: 保存済み実行結果]
    #   エラー: [OSError: 旧JSON履歴を読めない場合, ValueError: JSON構文が不正、配列以外、またはobject以外の要素を含む場合]
    # }
    def load(self) -> list[ExecutionResult]:
        if self.event_adapter is not None:
            records = self.event_adapter.records
        else:
            records = self._read_entries()
        if not isinstance(records, list):
            raise ValueError("execution_history.json must contain an array")
        return [
            ExecutionResult(
                str(record["action_id"]),
                bool(record["executed"]),
                str(record["mode"]),
                str(record.get("detail", "")),
            )
            for record in records
        ]

    # {
    #   責務: [append: 1件の実行結果をSession Journalまたは旧JSONへ保存する]
    #   処理: [Journal adapterがあればそのcommit契約を使い、未指定なら検証済みJSON配列へ追加して原子的に置換する]
    #   引数: [result: action ID・実行状態・入力方式・説明を持つ実行結果]
    #   戻り値: []
    #   エラー: [OSError: 旧JSONを書込めない場合, ValueError: 既存JSONの構文・配列形式・要素型が不正な場合]
    # }
    def append(self, result: ExecutionResult) -> None:
        record = asdict(result)
        if self.event_adapter is not None:
            self.event_adapter.prepare_append(record).publish()
            return
        entries = self._read_entries()
        entries.append(record)
        write_json_array_atomically(self.path, entries)

    # {
    #   責務: [_read_entries: 旧形式の実行履歴JSON配列を検証して読み込む]
    #   処理: [ファイル未作成時は空配列を返し、不正JSONと配列以外・object以外の要素を拒否する]
    #   引数: []
    #   戻り値: [list[dict[str, object]]: 検証済みの実行履歴項目]
    #   エラー: [ValueError: JSON構文が不正、配列以外、またはobject以外の要素を含む]
    # }
    def _read_entries(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []

        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError(f"invalid execution history JSON in {self.path}: {error}") from error
        if not isinstance(value, list):
            raise ValueError("execution_history.json must contain an array")
        if any(not isinstance(entry, dict) for entry in value):
            raise ValueError("execution_history.json entries must be objects")
        return value
