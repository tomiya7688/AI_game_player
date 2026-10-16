import json
from pathlib import Path

from ai_game_player.atomic_json import StagedJsonWrite, stage_json_write
from ai_game_player.journal_adapters import LegacyEventAdapter, StagedJournalAppend
from ai_game_player.models import ActionDecision, ScreenObservation


# {
#   責務: [HistoryStore: 画面観測と選択判断を追記形式のJSON履歴として管理する]
#   フィールド: [path: 旧判断履歴のJSON移行元, event_adapter: 現SessionのJournal writer]
# }
class HistoryStore:
    # {
    #   責務: [__init__: 判断履歴の保存先を設定する]
    #   処理: [旧判断履歴のJSON pathと、指定された場合はJournal移行adapterを保持する]
    #   引数: [path: 判断履歴JSONの旧保存先, event_adapter: Journal移行とappendを行うadapter]
    #   戻り値: []
    # }
    def __init__(self, path: Path, event_adapter: LegacyEventAdapter | None = None) -> None:
        self.path = path
        self.event_adapter = event_adapter

    # {
    #   責務: [append: 画面観測と判断を履歴JSONへ確定する]
    #   処理: [Journal adapterがあればSQLite appendを遅延し、未指定なら履歴全体を一時JSONへ準備する]
    #   引数: [observation: providerへ渡した画面観測, decision: 観測から選ばれた判断]
    #   戻り値: [なし: Journalまたは旧JSONへ判断履歴を確定する]
    #   エラー: [OSError: 履歴ファイルを書込みまたは置換できない, ValueError: 既存JSONが配列ではない]
    # }
    def append(self, observation: ScreenObservation, decision: ActionDecision) -> None:
        staged_write = self.prepare_append(observation, decision)
        try:
            staged_write.publish()
        finally:
            staged_write.discard()

    # {
    #   責務: [prepare_append: 画面観測と判断を含む次の履歴JSONを未確定状態で準備する]
    #   処理: [Journal利用時はevent appendを保留し、旧JSON利用時は既存配列へ1件追加した一時JSONを作る]
    #   引数: [observation: providerへ渡した画面観測, decision: 観測から選ばれた判断]
    #   戻り値: [StagedJsonWrite | StagedJournalAppend: 呼出側が保存世代を確認した後に公開する履歴]
    #   エラー: [OSError: 履歴ファイルを読込みまたは一時JSONを書込めない, ValueError: 既存JSONが配列ではない]
    # }
    def prepare_append(
        self,
        observation: ScreenObservation,
        decision: ActionDecision,
    ) -> StagedJsonWrite | StagedJournalAppend:
        record = {"observation": observation.to_dict(), "decision": decision.to_dict()}
        if self.event_adapter is not None:
            return self.event_adapter.prepare_append(record)
        entries = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries, list):
            raise ValueError("history.json must contain an array")
        entries.append(record)
        return stage_json_write(self.path, entries)
