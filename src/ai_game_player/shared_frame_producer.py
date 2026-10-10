"""Capture once per cadence and publish the shared frame to bounded subscribers."""

from __future__ import annotations

from math import isfinite
from threading import Event, Lock, Thread

from ai_game_player.frame_stream import (
    FrameCapture,
    FrameDeliveryMode,
    FramePacket,
    FrameProducerError,
    FrameProducerMetrics,
)
from ai_game_player.frame_subscriber import FrameSubscriber
from ai_game_player.screen_capture import ScreenFrame

__all__ = [
    "FrameCapture",
    "FrameDeliveryMode",
    "FramePacket",
    "FrameProducerError",
    "FrameProducerMetrics",
    "FrameSubscriber",
    "SharedFrameProducer",
]


# {
#   責務: [SharedFrameProducer: 1 capture backendを周期実行し、同じFramePacketをbounded subscriber queueへ配る]
#   フィールド: [capture_source: 現在のframe取得backend, interval_seconds: capture完了後に待つcadence秒数, subscribers: delivery mode別にframeを受け取るconsumer集合, source_generation: source変更で増える世代]
#   処理: [1: backendからframeを1回取得する, 2: producer内frame_idとsource世代を付与する, 3: queueを満たさないconsumerだけへ配り遅いconsumerを待たない]
# }
class SharedFrameProducer:
    # {
    #   責務: [__init__: capture backend、周期、metrics、thread lifecycleを初期化する]
    #   引数: [capture_source: ScreenFrameを同期取得するbackend, interval_seconds: 各capture後の待ち時間。小さい値ほどcapture cadenceが上がる]
    #   戻り値: []
    # }
    def __init__(self, capture_source: FrameCapture, interval_seconds: float = 1 / 15) -> None:
        if not callable(getattr(capture_source, "capture", None)):
            raise ValueError("capture_source must provide capture()")
        if isinstance(interval_seconds, bool) or not isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and greater than zero")
        self._capture_source = capture_source
        self.interval_seconds = interval_seconds
        self._lock = Lock()
        self._stop_event = Event()
        self._subscribers: set[FrameSubscriber] = set()
        self._thread: Thread | None = None
        self._started = False
        self._terminal = False
        self._error: Exception | None = None
        self._captured_frames = 0
        self._published_frames = 0
        self._dropped_frames = 0
        self._source_generation = 0
        self._last_frame_id = 0
        self._last_captured_at: float | None = None

    # {
    #   責務: [metrics: capture threadの最新集計をimmutable snapshotで返す]
    #   戻り値: [FrameProducerMetrics: capture数・配送数・破棄数・source世代・実行状態・失敗説明]
    # }
    @property
    def metrics(self) -> FrameProducerMetrics:
        with self._lock:
            return FrameProducerMetrics(
                captured_frames=self._captured_frames,
                published_frames=self._published_frames,
                dropped_frames=self._dropped_frames,
                source_generation=self._source_generation,
                last_frame_id=self._last_frame_id,
                is_running=self._thread is not None and self._thread.is_alive(),
                error_message=None if self._error is None else f"{type(self._error).__name__}: {self._error}",
            )

    # {
    #   責務: [subscribe: 新consumer用のbounded queueを登録する]
    #   引数: [mode: latest置換またはordered配送方針, max_pending: ORDERED queueの最大frame数。LATESTは常に1件だけ保持する]
    #   戻り値: [FrameSubscriber: frame取得とdrop数確認を行うsubscription]
    # }
    def subscribe(
        self,
        mode: FrameDeliveryMode = FrameDeliveryMode.LATEST,
        max_pending: int = 8,
    ) -> FrameSubscriber:
        with self._lock:
            if self._terminal or self._stop_event.is_set():
                raise RuntimeError("Cannot subscribe after the frame producer has stopped or is stopping")
            subscriber = FrameSubscriber(mode, max_pending, self._remove_subscriber)
            self._subscribers.add(subscriber)
            return subscriber

    # {
    #   責務: [start: producerの唯一のcapture threadを開始する]
    #   戻り値: []
    # }
    def start(self) -> None:
        with self._lock:
            if self._started or self._terminal:
                raise RuntimeError("Frame producer can only be started once")
            self._started = True
            self._thread = Thread(target=self._capture_loop, name="shared-frame-producer", daemon=True)
            self._thread.start()

    # {
    #   責務: [stop: capture threadへ停止を要求し、指定秒数以内に終了したか返す]
    #   引数: [timeout: thread終了を待つ秒数。Noneはthread終了まで待つ]
    #   戻り値: [bool: timeout内にcapture threadが終了したか]
    # }
    def stop(self, timeout: float | None = None) -> bool:
        if timeout is not None and (not isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be finite and non-negative")
        with self._lock:
            thread = self._thread
            self._stop_event.set()
            subscribers = self._finish_locked(None) if thread is None else []
        for subscriber in subscribers:
            subscriber._finish(None)
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    # {
    #   責務: [replace_source: backendを切り替え、旧sourceの未読frameを破棄して世代を進める]
    #   引数: [capture_source: 切替後にcapture()を呼び出すbackend]
    #   戻り値: [int: 切替後のsource_generation]
    # }
    def replace_source(self, capture_source: FrameCapture) -> int:
        if not callable(getattr(capture_source, "capture", None)):
            raise ValueError("capture_source must provide capture()")
        with self._lock:
            if self._terminal or self._stop_event.is_set():
                raise RuntimeError("Cannot replace a source after the frame producer has stopped or is stopping")
            self._capture_source = capture_source
            self._source_generation += 1
            generation = self._source_generation
            #   理由: [source世代更新とqueue flushを同じproducer lock内で行い、source切替完了後に旧frameが再配送される競合を防ぐ]
            for subscriber in self._subscribers:
                self._dropped_frames += subscriber._discard_pending()
            return generation

    # {
    #   責務: [_remove_subscriber: close済みconsumerを次回配送対象から外し、closeで破棄した数を記録する]
    #   引数: [subscriber: 登録解除するFrameSubscriber, dropped_frames: close時にqueueから破棄したframe数]
    #   戻り値: []
    # }
    def _remove_subscriber(self, subscriber: FrameSubscriber, dropped_frames: int) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
            self._dropped_frames += dropped_frames

    # {
    #   責務: [_notify_finished_subscribers: 終了理由をconsumerへ伝え、終了で捨てたqueue数を集計する]
    #   引数: [subscribers: 終了通知を受け取るconsumer, error: capture失敗時の元例外。正常停止ではNone]
    #   戻り値: []
    # }
    def _notify_finished_subscribers(
        self,
        subscribers: list[FrameSubscriber],
        error: Exception | None,
    ) -> None:
        dropped = sum(subscriber._finish(error) for subscriber in subscribers)
        with self._lock:
            self._dropped_frames += dropped

    # {
    #   責務: [_finish_locked: producerをterminal状態にし、登録consumerを終了通知対象として取り出す]
    #   引数: [error: capture失敗時の元例外。正常停止ではNone]
    #   戻り値: [list[FrameSubscriber]: 終了通知を受け取るconsumer]
    # }
    def _finish_locked(self, error: Exception | None) -> list[FrameSubscriber]:
        if self._terminal:
            return []
        self._terminal = True
        self._error = error
        subscribers = list(self._subscribers)
        self._subscribers.clear()
        return subscribers

    # {
    #   責務: [_capture_loop: cadenceごとにcaptureし、共通packetを各consumerへ配る]
    #   処理: [capture failureまたはstop要求でthreadを終了し、source変更中に返った旧frameは配送しない]
    #   戻り値: []
    # }
    def _capture_loop(self) -> None:
        error = None
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    capture_source = self._capture_source
                    source_generation = self._source_generation
                try:
                    frame = capture_source.capture()
                    if not isinstance(frame, ScreenFrame):
                        raise TypeError("capture() must return ScreenFrame")
                    if (
                        type(frame.width) is not int
                        or type(frame.height) is not int
                        or frame.width <= 0
                        or frame.height <= 0
                        or len(frame.bgra) != frame.width * frame.height * 4
                        or not isfinite(frame.captured_at)
                    ):
                        raise ValueError("capture() returned an invalid BGRA frame or timestamp")
                except Exception as capture_error:
                    with self._lock:
                        discard_error = (
                            source_generation != self._source_generation
                            or self._stop_event.is_set()
                        )
                        if discard_error:
                            self._dropped_frames += 1
                        else:
                            error = capture_error
                            subscribers = self._finish_locked(error)
                    if discard_error:
                        continue
                    self._notify_finished_subscribers(subscribers, error)
                    return
                with self._lock:
                    self._captured_frames += 1
                    self._last_frame_id += 1
                    frame_id = self._last_frame_id
                    discard_frame = (
                        source_generation != self._source_generation
                        or self._stop_event.is_set()
                    )
                    if discard_frame:
                        self._dropped_frames += 1
                        timestamp_error = None
                    elif (
                        self._last_captured_at is not None
                        and frame.captured_at < self._last_captured_at
                    ):
                        timestamp_error = ValueError(
                            "capture() timestamps must use a non-decreasing monotonic clock"
                        )
                        error = timestamp_error
                        subscribers = self._finish_locked(error)
                    else:
                        timestamp_error = None
                        self._last_captured_at = frame.captured_at
                        packet = FramePacket(frame_id, source_generation, frame)
                        for subscriber in self._subscribers:
                            published, dropped = subscriber._publish(packet)
                            self._published_frames += int(published)
                            self._dropped_frames += dropped
                if timestamp_error is not None:
                    self._notify_finished_subscribers(subscribers, error)
                    return
                if discard_frame:
                    continue
                self._stop_event.wait(self.interval_seconds)
        except Exception as capture_error:
            error = capture_error
        finally:
            with self._lock:
                subscribers = self._finish_locked(error)
            self._notify_finished_subscribers(subscribers, error)
