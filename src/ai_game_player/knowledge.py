import json
from pathlib import Path
from uuid import uuid4

from ai_game_player.atomic_json import write_json_array_atomically
# {
#   責務: [KnowledgeStore: ゲーム知識項目をJSON配列で永続化し検索する]
#   フィールド: [path: 知識ファイルの保存先]
# }
class KnowledgeStore:
    # {
    #   責務: [__init__: 知識ファイルの保存先を保持する]
    #   処理: [pathをインスタンスへ設定する]
    #   引数: [path: 知識JSONのパス]
    #   戻り値: []
    # }
    def __init__(self,path:Path): self.path=path

    # {
    #   責務: [add: 新しい知識項目を永続化する]
    #   処理: [一意IDを付けて既存項目へ追加し保存する]
    #   引数: [category: 分類, subject: 対象, statement: 内容, confidence: 確信度]
    #   戻り値: [dict[str, object]: 追加した知識項目]
    # }
    def add(self,category:str,subject:str,statement:str,confidence:float=1.0)->dict[str,object]:
        entries=self._read(); entry={"id":uuid4().hex,"category":category,"subject":subject,"statement":statement,"confidence":confidence}; entries.append(entry); self._write(entries); return entry
    # {
    #   責務: [search: 内容と任意の分類で知識項目を検索する]
    #   処理: [subjectとstatementを大文字小文字を無視して部分一致検索する]
    #   引数: [query: 検索語, category: 任意の分類]
    #   戻り値: [list[dict[str, object]]: 一致した知識項目]
    # }
    def search(self,query:str,category:str|None=None)->list[dict[str,object]]:
        needle=query.casefold(); return [e for e in self._read() if (category is None or e.get("category")==category) and needle in (str(e.get("subject",""))+" "+str(e.get("statement",""))).casefold()]
    # {
    #   責務: [_read: 知識ファイルを配列として読み込む]
    #   処理: [未作成なら空配列を返し、JSON配列であることを検証する]
    #   引数: []
    #   戻り値: [list[dict[str, object]]: 読み込んだ知識項目]
    #   エラー: [配列以外のJSONでValueError]
    # }
    def _read(self)->list[dict[str,object]]:
        if not self.path.exists(): return []
        value=json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value,list): raise ValueError("knowledge.json must contain an array")
        return value
    # {
    #   責務: [_write: 知識項目一覧を原子的に保存する]
    #   処理: [共通JSON writerで配列を置換する]
    #   引数: [entries: 保存する知識項目]
    #   戻り値: []
    # }
    def _write(self,entries:list[dict[str,object]])->None:
        write_json_array_atomically(self.path, entries)
