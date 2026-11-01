"""通常ファイルだけを、FIFO待ちや容量超過なしで限定読込する。"""

import os
from pathlib import Path
import stat


# {
#   責務: [read_regular_file: 非通常ファイルや途中の差し替えを検査し、上限付きbytesだけを読む]
#   処理: [開く前と開いたFDの種別を確認し、POSIXではnonblocking/no-followで開く。
#     実サイズと読込量の両方を制限し、途中で増大したファイルも拒否する]
#   引数: [path: ローカルの通常ファイル, max_bytes: 正整数の許可byte上限]
#   戻り値: [bytes: 上限以内の内容]
#   エラー: [ValueError: 上限・ファイル種別・容量が不正, OSError: 開く/読む操作の失敗]
# }
def read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("regular file read requires a positive byte limit")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("bounded read requires a regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("bounded read opened a non-regular file")
        if info.st_size > max_bytes:
            raise ValueError("regular file exceeds read limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("regular file exceeds read limit")
        return payload
    finally:
        os.close(descriptor)
