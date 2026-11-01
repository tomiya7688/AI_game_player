"""legacy JSONのplanned traceを、世代guard内で最新履歴へ追記する遅延操作。"""

from collections.abc import Callable
import json
from typing import Any


# {
#   責務: [DeferredTraceAppend: 計画準備後に追記された実行auditを失わず判断traceを公開する]
#   フィールド: [_payload: 準備時に固定した一件のJSON bytes,
#     _append_record: guard内で最新履歴へ追記する操作, _finished: 二重公開・破棄後公開の防止状態]
# }
class DeferredTraceAppend:
    # {
    #   責務: [__init__: 公開する一件だけを準備し、履歴全体の古いsnapshotを保持しない]
    #   処理: [recordをJSONに固定し、publishまでファイルを書き換えない]
    #   引数: [record: planned trace一件, append_record: 最新の保存先へ追記する呼出先]
    #   戻り値: [なし]
    #   エラー: [ValueError/TypeError: recordをJSON化できない]
    # }
    def __init__(
        self, record: dict[str, Any], append_record: Callable[[dict[str, Any]], None]
    ) -> None:
        self._payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
        self._append_record = append_record
        self._finished = False

    # {
    #   責務: [publish: 世代guardを保持した呼出側から、最新履歴へ一件を公開する]
    #   処理: [二重実行を拒否し、準備済みrecordだけを最新履歴へ渡す]
    #   引数: [なし]
    #   戻り値: [なし]
    #   エラー: [RuntimeError: 公開済み/破棄済み, OSError: 保存失敗]
    # }
    def publish(self) -> None:
        if self._finished:
            raise RuntimeError("trace append is already finished")
        self._finished = True
        self._append_record(json.loads(self._payload))

    # {
    #   責務: [discard: 停止・公開後に準備内容を再利用できない状態へする]
    #   処理: [ファイル操作なしで終了状態を設定する]
    #   引数: [なし]
    #   戻り値: [なし]
    # }
    def discard(self) -> None:
        self._finished = True
