"""Bounded queues that isolate consumer cadence from capture cadence."""

from __future__ import annotations

from collections import deque
from math import isfinite
from threading import Condition
from time import monotonic
from typing import Callable

from ai_game_player.frame_stream import FrameDeliveryMode, FramePacket, FrameProducerError


# {
#   責務: [FrameSubscriber: 1 consumer分のbounded frame queueと破棄数を保持する]
#   フィールド: [mode: latest置換または順序維持の配送方針, max_pending: queueが保持できる最大frame数, dropped_frames: queue方針により破棄したframe数]
# }
class FrameSubscriber:
    # {
    #   責務: [__init__: consumerの配送方針と空のbounded queueを初期化する]
    #   引数: [mode: queue満杯時にlatestへ置換するか既存順序を保つか, max_pending: ORDERED queueの最大frame数, on_close: producerへ解除対象と破棄数を返すcallback]
    #   戻り値: []
    # }
    def __init__(
        self,
        mode: FrameDeliveryMode,
        max_pending: int,
        on_close: Callable[[FrameSubscriber, int], None],
    ) -> None:
        if not isinstance(mode, FrameDeliveryMode):
            raise ValueError("mode must be a FrameDeliveryMode")
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending <= 0:
            raise ValueError("max_pending must be a positive integer")
        self.mode = mode
        self.max_pending = 1 if mode is FrameDeliveryMode.LATEST else max_pending
        self._on_close = on_close
        self._condition = Condition()
        self._frames: deque[FramePacket] = deque()
        self._closed = False
        self._error: Exception | None = None
        self._dropped_frames = 0

    # {
    #   責務: [dropped_frames: queueが捨てたframe数をthread-safeに返す]
    #   戻り値: [int: subscription作成後の累積破棄数]
    # }
    @property
    def dropped_frames(self) -> int:
        with self._condition:
            return self._dropped_frames

    # {
    #   責務: [pending_frames: consumerがまだ取得していないframe数を返す]
    #   戻り値: [int: bounded queue内のframe数]
    # }
    @property
    def pending_frames(self) -> int:
        with self._condition:
            return len(self._frames)

    # {
    #   責務: [is_closed: subscriptionが停止済みかをthread-safeに返す]
    #   戻り値: [bool: producer停止またはconsumer closeが完了しているか]
    # }
    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    # {
    #   責務: [get: 指定timeout内にframeまたはstream終了を返す]
    #   引数: [timeout: frame到着を待つ秒数。Noneは終了またはframe到着まで待つ]
    #   戻り値: [FramePacket: 次に処理するframe, None: stream正常終了]
    # }
    def get(self, timeout: float | None = None) -> FramePacket | None:
        if timeout is not None and (not isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and non-negative")
        deadline = None if timeout is None else monotonic() + timeout
        with self._condition:
            while not self._frames and not self._closed:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._frames:
                return self._frames.popleft()
            if self._error is not None:
                raise FrameProducerError(
                    f"Frame capture stopped after {type(self._error).__name__}: {self._error}"
                ) from self._error
            return None

    # {
    #   責務: [_publish: producerがpacketをqueue方針に従ってnon-blocking配送する]
    #   引数: [packet: 同じcapture結果を共有するframe envelope]
    #   戻り値: [tuple[bool, int]: queueへ追加できたかと、この配送で破棄したframe数]
    # }
    def _publish(self, packet: FramePacket) -> tuple[bool, int]:
        with self._condition:
            if self._closed:
                return False, 0
            dropped = 0
            if self.mode is FrameDeliveryMode.LATEST:
                dropped = len(self._frames)
                self._frames.clear()
            elif len(self._frames) >= self.max_pending:
                dropped = 1
                self._dropped_frames += dropped
                return False, dropped
            self._frames.append(packet)
            self._dropped_frames += dropped
            self._condition.notify()
            return True, dropped

    # {
    #   責務: [_discard_pending: source切替時に旧sourceの未読frameをqueueから外す]
    #   戻り値: [int: queueから破棄したframe数]
    # }
    def _discard_pending(self) -> int:
        with self._condition:
            dropped = len(self._frames)
            self._frames.clear()
            self._dropped_frames += dropped
            return dropped

    # {
    #   責務: [_finish: producer終了状態と失敗理由をconsumerへ通知する]
    #   引数: [error: capture失敗時の元例外。正常停止ではNone, discard_pending: 終了時に未読queueを破棄するか]
    #   戻り値: [int: 終了通知時に破棄したframe数]
    # }
    def _finish(self, error: Exception | None, *, discard_pending: bool = False) -> int:
        with self._condition:
            dropped = 0
            if error is not None or discard_pending:
                dropped = len(self._frames)
                self._frames.clear()
                self._dropped_frames += dropped
            self._error = error
            self._closed = True
            self._condition.notify_all()
            return dropped

    # {
    #   責務: [close: subscriptionを終了し、producerへの以後の配送を止める]
    #   戻り値: []
    # }
    def close(self) -> None:
        dropped = self._finish(None, discard_pending=True)
        self._on_close(self, dropped)
