import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
        os.replace(self.temporary_path, self.target_path)

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
#   処理: [保存先と同じディレクトリの一時ファイルへUTF-8 JSONを書き、内容を同期してから確定・破棄操作を返す]
#   引数: [target_path: 最終的なJSON保存先, value: JSONへ変換する内容]
#   戻り値: [StagedJsonWrite: 一時内容の公開または破棄に使う操作]
#   エラー: [OSError: 親ディレクトリまたは一時ファイルを作成・書込みできない, TypeErrorまたはValueError: valueをJSONへ変換できない]
# }
def stage_json_write(target_path: Path, value: object) -> StagedJsonWrite:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(value, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    assert temporary_path is not None
    return StagedJsonWrite(target_path, temporary_path)


# {
#   責務: [write_json_array_atomically: JSON配列を一時ファイル経由で置換する]
#   処理: [共有stage_json_writeで配列を準備し、置換後または失敗後に一時ファイルを後始末する]
#   引数: [path: 最終的な配列JSONの保存先, entries: 保存する配列要素]
#   戻り値: [なし: 保存先のJSON配列を原子的に置換する]
#   エラー: [TypeErrorまたはValueError: 配列要素をJSONへ変換できない, OSError: 一時ファイルの書込み・同期・置換に失敗する]
# }
def write_json_array_atomically(path: Path, entries: list[Any]) -> None:
    staged_write = stage_json_write(path, entries)
    try:
        staged_write.publish()
    finally:
        try:
            staged_write.discard()
        except OSError:
            pass
