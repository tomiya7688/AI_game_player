"""Mode metadataの有界queueと記録IO。実行停止のlockを保持しない。"""

from collections import deque
from collections.abc import Callable
from pathlib import Path
import threading

from ai_game_player.atomic_json import stage_json_write
from ai_game_player.event_journal import EventJournal
from ai_game_player.runtime_mode_state import RuntimeModeSnapshot


# {
#   責務: [RuntimeModeRecorder: Mode/Phase metadataの有界記録待ちとJournal/checkpoint IOを所有する]
#   フィールド: [journal: 既存Session Journal, state_path: 任意checkpoint先, capacity: 待ち件数上限,
#     _pending/_queue_lock: IOと分離したmetadata queue, _io_lock: 複数flushだけを直列化するlock]
# }
class RuntimeModeRecorder:
    # {
    #   責務: [__init__: 記録先と有界queueを初期化する]
    #   処理: [既存Journalを保持し、workerやOS入力を起動しない]
    #   引数: [journal: 任意Journal, state_path: 任意JSON保存先, capacity: 記録待ち上限1〜4096]
    #   戻り値: [なし]
    #   エラー: [ValueError: capacityが範囲外]
    # }
    def __init__(
        self, journal: EventJournal | None, state_path: Path | None, capacity: int
    ) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError("runtime mode record capacity is invalid")
        self.journal = journal
        self.state_path = None if state_path is None else Path(state_path)
        self.capacity = capacity
        self._pending: deque[tuple[str, dict]] = deque()
        self._queue_lock = threading.Lock()
        self._io_lock = threading.Lock()

    # {
    #   責務: [full: 通常metadataを追加する余地があるか照合する]
    #   処理: [Journalがある場合だけqueue長を上限と比較する]
    #   引数: [self: 対象Recorder]
    #   戻り値: [bool: Journal記録待ちが満杯ならTrue]
    # }
    @property
    def full(self) -> bool:
        with self._queue_lock:
            return self.journal is not None and len(self._pending) >= self.capacity

    # {
    #   責務: [enqueue: IOなしで一件のmetadataを記録待ちにする]
    #   処理: [Journalなしなら保存せず、満杯時に古い記録を黙って上書きしない]
    #   引数: [event_id: 再試行にも使う一意ID, payload: Controllerが新しく構成したmetadata辞書]
    #   戻り値: [bool: 記録待ちへ追加できた場合True。JournalなしはTrue、満杯はFalse]
    # }
    def enqueue(self, event_id: str, payload: dict) -> bool:
        if self.journal is None:
            return True
        with self._queue_lock:
            if len(self._pending) >= self.capacity:
                return False
            self._pending.append((event_id, payload))
            return True

    # {
    #   責務: [flush: 記録待ちを既存Journalへ送り最新状態を原子的に保存する]
    #   処理: [queue lock外でIOを実行し、成功したeventだけ除去する。再試行時は同じevent IDを使う]
    #   引数: [snapshot: 最新metadataを短時間で返す関数。queue lockを保持して呼ばない]
    #   戻り値: [bool: 別flushや未処理eventが残る場合False、全件記録した場合True]
    #   エラー: [Exception: Journal/JSON保存失敗。呼出ControllerがERROR/Stopへ反映する]
    # }
    def flush(self, snapshot: Callable[[], RuntimeModeSnapshot]) -> bool:
        if not self._io_lock.acquire(blocking=False):
            return False
        try:
            for _ in range(self.capacity):
                with self._queue_lock:
                    pending = self._pending[0] if self._pending else None
                if pending is None:
                    break
                event_id, payload = pending
                if self.journal is not None:
                    self.journal.append(
                        "runtime.mode_state",
                        event_id=event_id,
                        status="applied",
                        payload=payload,
                    )
                with self._queue_lock:
                    if self._pending and self._pending[0][0] == event_id:
                        self._pending.popleft()
            if self.state_path is not None:
                staged = stage_json_write(self.state_path, snapshot().to_dict())
                try:
                    staged.publish()
                finally:
                    staged.discard()
            with self._queue_lock:
                return not self._pending
        finally:
            self._io_lock.release()
