import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


# {
#   責務: [StagedJsonWrite: JSON内容を一時ファイルへ準備し、呼出側の確定時に置換する]
#   フィールド: [target_path: 確定先JSON, temporary_path: 同じディレクトリに作る未確定ファイル]
# }
@dataclass(frozen=True)
class StagedJsonWrite:
    target_path: Path
    temporary_path: Path

    # {
    #   責務: [publish: 準備済みJSONを保存先へ公開する]
    #   処理: [同一ディレクトリ内の一時ファイルを保存先へ置換する]
    #   引数: []
    #   戻り値: []
    #   エラー: [OSError: 一時ファイルを保存先へ置換できない]
    # }
    def publish(self) -> None:
        self.temporary_path.replace(self.target_path)

    # {
    #   責務: [discard: 確定に使わなかった一時JSONを破棄する]
    #   処理: [指定された一時ファイルだけを削除し、既に公開済みなら何もしない]
    #   引数: []
    #   戻り値: []
    #   エラー: [OSError: 一時ファイルを削除できない]
    # }
    def discard(self) -> None:
        self.temporary_path.unlink(missing_ok=True)


# {
#   責務: [stage_json_write: JSONを保存先と同じディレクトリの一時ファイルへ準備する]
#   処理: [UTF-8のJSONを一時ファイルへ書き、確定・破棄操作を返す]
#   引数: [target_path: 最終的なJSON保存先, value: JSONへ変換する内容]
#   戻り値: [StagedJsonWrite: 一時内容の公開または破棄に使う操作]
#   エラー: [OSError: 親ディレクトリまたは一時ファイルを作成・書込みできない, TypeErrorまたはValueError: valueをJSONへ変換できない]
# }
def stage_json_write(target_path: Path, value: object) -> StagedJsonWrite:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_suffix(f".{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return StagedJsonWrite(target_path, temporary_path)
