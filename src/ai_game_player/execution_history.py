import json
from pathlib import Path

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.journal_adapters import LegacyEventAdapter


# {
#   責務: [ExecutionHistory: 実行結果を旧JSON互換readerまたはSession Event Journalで管理する]
#   フィールド: [path: 旧JSON履歴の移行元, event_adapter: 現SessionのJournal writerと互換cache]
# }
class ExecutionHistory:
    # {
    #   責務: [__init__: 旧履歴の保存先と任意のJournal adapterを関連付ける]
    #   処理: [adapterの初期化時に実行結果JSONの検証・移行を完了させる]
    #   引数: [path: execution_history.jsonの旧保存先, event_adapter: 現SessionのJournalへ移行するadapter]
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
    #   処理: [Journal adapterのcompatibility cacheまたは旧JSON配列をExecutionResultへ変換する]
    #   引数: [self: 実行履歴store]
    #   戻り値: [list[ExecutionResult]: 保存済み実行結果]
    #   エラー: [OSError: 旧JSON履歴を読めない場合, ValueError: JSONが不正または配列でない場合]
    # }
    def load(self) -> list[ExecutionResult]:
        if self.event_adapter is not None:
            records = self.event_adapter.records
        elif not self.path.exists():
            return []
        else:
            records = json.loads(self.path.read_text(encoding="utf-8"))
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
    #   処理: [Journal adapterがあればcommitし、未指定なら既存JSON配列へ追加する]
    #   引数: [result: action ID・実行状態・入力方式・説明を持つ実行結果]
    #   戻り値: []
    #   エラー: [OSError: 旧JSONを書込めない場合, ValueError: 既存JSONが配列ではない場合]
    # }
    def append(self, result: ExecutionResult) -> None:
        record = result.__dict__.copy()
        if self.event_adapter is not None:
            self.event_adapter.prepare_append(record).publish()
            return
        entries = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries, list):
            raise ValueError("execution_history.json must contain an array")
        entries.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
