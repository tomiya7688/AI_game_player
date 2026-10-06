from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


# {
#   責務: [write_json_array_atomically: JSON配列を一時ファイル経由で安全に置換する]
#   処理: [親ディレクトリを作成し、一時ファイルへ書き込み・同期してから置換する]
#   引数: [path: 保存先, entries: JSON配列要素]
#   戻り値: []
#   エラー: [書き込み・同期・置換に失敗すると例外を送出する]
# }
def write_json_array_atomically(path: Path, entries: list[Any]) -> None:
    """Write a JSON array through a flushed same-directory temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(entries, ensure_ascii=False, indent=2)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(encoded)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
