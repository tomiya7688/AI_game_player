import json
from pathlib import Path

from ai_game_player.atomic_json import StagedJsonWrite, stage_json_write
from ai_game_player.models import ActionDecision, ScreenObservation


# {
#   責務: [HistoryStore: 画面観測と選択判断を追記形式のJSON履歴として管理する]
#   フィールド: [path: 判断履歴を保存するJSONファイル]
# }
class HistoryStore:
    # {
    #   責務: [__init__: 判断履歴の保存先を設定する]
    #   処理: [指定されたJSONファイルのパスを保持する]
    #   引数: [path: 判断履歴JSONの保存先]
    #   戻り値: []
    # }
    def __init__(self, path: Path) -> None:
        self.path = path

    # {
    #   責務: [append: 画面観測と判断を履歴JSONへ確定する]
    #   処理: [履歴全体を一時ファイルに準備し、保存先へ原子的に置換する]
    #   引数: [observation: providerへ渡した画面観測, decision: 観測から選ばれた判断]
    #   戻り値: []
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
    #   処理: [既存の履歴配列を読み、1件追加したJSONを同じ保存先ディレクトリへ書く]
    #   引数: [observation: providerへ渡した画面観測, decision: 観測から選ばれた判断]
    #   戻り値: [StagedJsonWrite: 呼出側が保存世代を確認した後に公開する一時JSON]
    #   エラー: [OSError: 履歴ファイルを読込みまたは一時JSONを書込めない, ValueError: 既存JSONが配列ではない]
    # }
    def prepare_append(
        self,
        observation: ScreenObservation,
        decision: ActionDecision,
    ) -> StagedJsonWrite:
        entries = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries, list):
            raise ValueError("history.json must contain an array")
        entries.append({"observation": observation.to_dict(), "decision": decision.to_dict()})
        return stage_json_write(self.path, entries)
