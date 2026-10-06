import json
from pathlib import Path

from ai_game_player.atomic_json import write_json_array_atomically
from ai_game_player.models import ActionDecision, ScreenObservation
# {
#   責務: [HistoryStore: 観測と判断の履歴をJSON配列で永続化する]
#   フィールド: [path: 履歴ファイルの保存先]
# }
class HistoryStore:
    # {
    #   責務: [__init__: 履歴ファイルの保存先を保持する]
    #   処理: [pathをインスタンスへ設定する]
    #   引数: [path: 履歴JSONのパス]
    #   戻り値: []
    # }
    def __init__(self,path:Path): self.path=path

    # {
    #   責務: [append: 観測と判断を履歴へ追記する]
    #   処理: [既存配列を読み、シリアライズした項目を原子的に保存する]
    #   引数: [observation: 画面観測, decision: 選択判断]
    #   戻り値: []
    #   エラー: [保存形式が配列でない場合ValueError]
    # }
    def append(self,observation:ScreenObservation,decision:ActionDecision)->None:
        entries=json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
        if not isinstance(entries,list): raise ValueError("history.json must contain an array")
        entries.append({"observation":observation.to_dict(),"decision":decision.to_dict()})
        write_json_array_atomically(self.path, entries)
