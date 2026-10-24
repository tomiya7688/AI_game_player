"""2048観測の処理別経過時間を保持する。計測だけを行い、安全期限は更新しない。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import time


class ObservationTiming:
    """一回の観測要求の累積処理時間と実行中の処理を、単調時計で計測する。"""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self.clock = clock
        self.started_at = clock()
        self.stage_seconds: dict[str, float] = {}
        self.active_stage: str | None = None

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        """直列処理の名前を受け取り、例外時も処理時間を加算して例外を再送出する。"""
        started_at = self.clock()
        self.active_stage = stage
        try:
            yield
        finally:
            self.stage_seconds[stage] = self.stage_seconds.get(stage, 0.0) + self.clock() - started_at
            self.active_stage = None

    def snapshot(self) -> dict[str, object]:
        """要求開始からの経過ms、完了した区間の累積ms、実行中の区間名を返す。"""
        return {
            "elapsed_ms": round((self.clock() - self.started_at) * 1000, 3),
            "stage_ms": {stage: round(seconds * 1000, 3) for stage, seconds in self.stage_seconds.items()},
            "active_stage": self.active_stage,
        }
