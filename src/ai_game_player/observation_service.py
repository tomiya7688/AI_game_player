from collections.abc import Callable
from dataclasses import replace
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Protocol
from uuid import uuid4

from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture
from ai_game_player.shared_frame_producer import (
    FrameDeliveryMode,
    FramePacket,
    FrameProducerError,
    FrameSubscriber,
    SharedFrameProducer,
)

DEFAULT_FRAME_CAPTURE_INTERVAL_SECONDS = 1 / 15
DEFAULT_FRAME_WAIT_TIMEOUT_SECONDS = 2.0
DEFAULT_MAXIMUM_FRAME_AGE_SECONDS = 1.0
NANOSECONDS_PER_SECOND = 1_000_000_000


# {
#   責務: [WindowCaptureSource: 1つのWindows window handleからSharedFrameProducerが使う画面frameを取得する]
#   フィールド: [capture_frame: OSまたはfake capture関数, window_handle: このsourceが取得する対象window]
# }
class WindowCaptureSource:
    # {
    #   責務: [__init__: 対象windowと差替え可能なcapture関数を保持する]
    #   引数: [capture_frame: window handleを受け取りScreenFrameを返すcapture関数, window_handle: frameを取得する対象window。Noneはdesktop]
    #   戻り値: []
    # }
    def __init__(
        self,
        capture_frame: Callable[[int | None], ScreenFrame],
        window_handle: int | None,
    ) -> None:
        self._capture_frame = capture_frame
        self.window_handle = window_handle

    # {
    #   責務: [capture: SharedFrameProducerへ次の対象window frameを同期取得して返す]
    #   戻り値: [ScreenFrame: BGRA画像とcapture時のmonotonic時刻を持つframe]
    # }
    def capture(self) -> ScreenFrame:
        return self._capture_frame(self.window_handle)


# {
#   責務: [FrameAnalyzerFactory: OCR設定を持つ画面frame解析器を生成する境界]
# }
class FrameAnalyzerFactory(Protocol):
    # {
    #   責務: [__call__: OCR recognizerを受け取りframe analyzerを生成する]
    #   処理: [Windows capture frameをObservationへ変換するAnalyzerを返す]
    #   引数: [ocr_recognizer: Analyzerが文字認識に使うoptional recognizer]
    #   戻り値: [FrameAnalyzer: analyze(frame, screen_id)を提供する解析器]
    # }
    def __call__(self, ocr_recognizer: object | None) -> "FrameAnalyzer": ...


# {
#   責務: [FrameAnalyzer: capture frameを画面状態へ変換する契約]
# }
class FrameAnalyzer(Protocol):
    # {
    #   責務: [analyze: frameの画面内容とOCR textをObservationへ変換する]
    #   処理: [呼び出し元が指定したscreen IDを変換後のObservationへ付ける]
    #   引数: [frame: Windows capture backendが取得した画面, screen_id: Observationの識別名]
    #   戻り値: [ScreenObservation: frameから生成した画面状態]
    # }
    def analyze(self, frame: ScreenFrame, screen_id: str) -> ScreenObservation: ...


# {
#   責務: [LiveObservationService: bounded shared frame streamから選択画面を解析し、frame IDとmonotonic時刻付きObservationを返す]
#   フィールド: [capture_frame: 対象windowからframeを取得する関数, _windows_capture: default Windows backendとWGC resource lifetime, analyzer_factory: frame解析依存の生成関数, ocr_factory: optional OCR recognizer生成関数, producer/subscriber: Capture cadenceをObservation samplingから分離する共有stream]
# }
class LiveObservationService:
    # {
    #   責務: [__init__: 差替え可能なcapture・frame analyzer・OCR依存とbounded frame streamの時間条件を初期化する]
    #   処理: [未指定capture依存では1つのWindowsScreenCaptureを保持し、WGC sessionをcapture thread間で再利用する。cadenceとfreshness上限も検証する]
    #   引数: [capture_frame: window handleからScreenFrameを取得する関数, analyzer_factory: OCR依存からanalyzerを作る関数, ocr_factory: optional OCR recognizerを作る関数, capture_interval_seconds: producerがcaptureを繰り返す間隔, frame_wait_timeout_seconds: consumerがframeを待つ上限, maximum_frame_age_seconds: Observationに利用できるframeの最大経過時間]
    #   戻り値: []
    # }
    def __init__(
        self,
        capture_frame: Callable[[int | None], ScreenFrame] | None = None,
        analyzer_factory: Callable[[object | None], FrameAnalyzer] | None = None,
        ocr_factory: Callable[[], object | None] | None = None,
        *,
        capture_interval_seconds: float = DEFAULT_FRAME_CAPTURE_INTERVAL_SECONDS,
        frame_wait_timeout_seconds: float = DEFAULT_FRAME_WAIT_TIMEOUT_SECONDS,
        maximum_frame_age_seconds: float = DEFAULT_MAXIMUM_FRAME_AGE_SECONDS,
    ) -> None:
        for value, field_name in (
            (capture_interval_seconds, "capture_interval_seconds"),
            (frame_wait_timeout_seconds, "frame_wait_timeout_seconds"),
            (maximum_frame_age_seconds, "maximum_frame_age_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be finite and greater than zero")
        if capture_frame is None:
            self._windows_capture = WindowsScreenCapture()
            self.capture_frame = self._windows_capture.capture
        else:
            self._windows_capture = None
            self.capture_frame = capture_frame
        self.analyzer_factory = (
            analyzer_factory
            if analyzer_factory is not None
            else self._create_frame_analyzer
        )
        self.ocr_factory = ocr_factory if ocr_factory is not None else self._create_ocr_recognizer
        self.capture_interval_seconds = capture_interval_seconds
        self.frame_wait_timeout_seconds = frame_wait_timeout_seconds
        self.maximum_frame_age_seconds = maximum_frame_age_seconds
        self._producer: SharedFrameProducer | None = None
        self._subscriber: FrameSubscriber | None = None
        self._window_handle: int | None = None
        self._stream_id: str | None = None
        self._lock = Lock()
        self._closed = False

    # {
    #   責務: [capture_observation: 選択windowのfresh frameを共有streamから取得し、live Observationへ変換する]
    #   処理: [latest subscriberからframeを取得し、同じframeの識別子とcapture時刻をfeatures.capture_metadataへ保存する]
    #   引数: [window_handle: capture対象のWindows window handle、省略時はcapture backendのdesktop動作]
    #   戻り値: [ScreenObservation: screen状態とframe_id・monotonic_nsを含むcapture metadata]
    #   エラー: [capture停止・timeout・stale frame・OCR・解析失敗: 呼び出し元が表示またはSession停止を判断できるよう送出]
    # }
    def capture_observation(self, window_handle: int | None) -> ScreenObservation:
        packet, stream_id = self._get_latest_frame_packet(window_handle)
        analyzer = self.analyzer_factory(self.ocr_factory())
        observation = analyzer.analyze(packet.frame, "live")
        features = dict(observation.features)
        features["capture_metadata"] = {
            "frame_id": f"{stream_id}:{packet.source_generation}:{packet.frame_id}",
            "source_generation": packet.source_generation,
            "monotonic_ns": round(packet.frame.captured_at * NANOSECONDS_PER_SECOND),
        }
        return replace(observation, features=features)

    # {
    #   責務: [subscribe_frames: Observationと同じcapture streamを使う追加consumerへbounded subscriptionを渡す]
    #   処理: [対象windowをproducerへ設定してから、consumer指定のlatestまたはordered queueを登録する]
    #   引数: [window_handle: frameを取得する対象window, mode: latest置換または順序維持の配送方針, max_pending: ORDERED queueの保持上限]
    #   戻り値: [FrameSubscriber: Observationと同じFramePacket列を読むsubscription]
    #   エラー: [service終了後またはproducer停止後に登録した場合: capture不能を呼び出し元へ送出]
    # }
    def subscribe_frames(
        self,
        window_handle: int | None,
        mode: FrameDeliveryMode = FrameDeliveryMode.ORDERED,
        max_pending: int = 8,
    ) -> FrameSubscriber:
        with self._lock:
            if self._closed:
                raise RuntimeError("LiveObservationService is closed and cannot create a frame subscription")
            self._ensure_frame_producer(window_handle)
            producer = self._producer
            if producer is None:
                raise RuntimeError("LiveObservationService did not create a frame producer")
            return producer.subscribe(mode, max_pending)

    # {
    #   責務: [_get_latest_frame_packet: 選択windowのbounded latest subscriptionから期限内のframeを得る]
    #   処理: [window変更時はproducer sourceを交換し、stale frameを捨てながらtimeoutまで最新frameを待つ]
    #   引数: [window_handle: frameを取得する対象window、省略時はdesktop]
    #   戻り値: [tuple[FramePacket, str]: producer識別子付きframe packet]
    #   エラー: [producer停止・capture失敗・期限内にfresh frameを取得できない場合: 呼び出し元が失敗を処理できるよう送出]
    # }
    def _get_latest_frame_packet(self, window_handle: int | None) -> tuple[FramePacket, str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("LiveObservationService is closed and cannot capture another frame")
            self._ensure_frame_producer(window_handle)
            subscriber = self._subscriber
            stream_id = self._stream_id
            if subscriber is None or stream_id is None:
                raise RuntimeError("LiveObservationService did not create a frame subscription")

            deadline = monotonic() + self.frame_wait_timeout_seconds
            stale_frame_seen = False
            while True:
                remaining_seconds = deadline - monotonic()
                packet = None
                if remaining_seconds > 0:
                    try:
                        packet = subscriber.get(timeout=remaining_seconds)
                    except FrameProducerError:
                        self._stop_frame_producer()
                        raise
                if packet is None:
                    if stale_frame_seen:
                        raise TimeoutError(
                            "観測の待ち時間内に新しい画面frameを取得できませんでした。"
                            "対象ウィンドウを選び直してから画面取得を再試行してください。"
                        )
                    raise TimeoutError(
                        "画面frameを取得できませんでした。対象ウィンドウが有効か確認し、"
                        "画面取得を再試行してください。"
                    )
                frame_age_seconds = monotonic() - packet.frame.captured_at
                if frame_age_seconds < 0:
                    raise ValueError(
                        "画面frameのmonotonic取得時刻が現在時刻より後になっています。"
                        "capture backendのtimestamp実装を確認してください。"
                    )
                if frame_age_seconds > self.maximum_frame_age_seconds:
                    stale_frame_seen = True
                    continue
                return packet, stream_id

    # {
    #   責務: [_ensure_frame_producer: 選択windowへ結び付いたlatest-frame producerとsubscriptionを用意する]
    #   処理: [初回またはcapture失敗後にproducerを開始し、window変更時はsource世代を進めて未読旧frameを捨てる]
    #   引数: [window_handle: 次に配信するframeの対象window、省略時はdesktop]
    #   戻り値: []
    #   エラー: [producerが停止中またはsourceを交換できない場合: capture失敗として呼び出し元へ送出]
    # }
    def _ensure_frame_producer(self, window_handle: int | None) -> None:
        if self._producer is not None and self._producer.metrics.error_message is not None:
            self._stop_frame_producer()
        if self._producer is None:
            source = WindowCaptureSource(self.capture_frame, window_handle)
            producer = SharedFrameProducer(source, interval_seconds=self.capture_interval_seconds)
            subscriber = producer.subscribe(FrameDeliveryMode.LATEST)
            try:
                producer.start()
            except Exception:
                subscriber.close()
                producer.stop()
                raise
            self._producer = producer
            self._subscriber = subscriber
            self._window_handle = window_handle
            self._stream_id = uuid4().hex
            return
        if self._window_handle != window_handle:
            source = WindowCaptureSource(self.capture_frame, window_handle)
            self._producer.replace_source(source)
            self._window_handle = window_handle

    # {
    #   責務: [_stop_frame_producer: latest subscriptionを解除し、capture threadへ停止を要求する]
    #   処理: [queue上の未読frameを破棄し、producerがcapture失敗またはclose時に再利用されない状態へ戻す]
    #   戻り値: [bool: producer threadが停止済みならTrue、thread未生成ならTrue]
    # }
    def _stop_frame_producer(self) -> bool:
        subscriber = self._subscriber
        producer = self._producer
        if subscriber is not None:
            subscriber.close()
        stopped = producer.stop(timeout=self.frame_wait_timeout_seconds) if producer is not None else True
        if not stopped:
            return False
        self._producer = None
        self._subscriber = None
        self._window_handle = None
        self._stream_id = None
        return True

    # {
    #   責務: [close: ObservationServiceのframe subscription、capture thread、Native WGC資源を終了する]
    #   処理: [producer停止後にWindowsScreenCaptureのWGC sessionとNative Runtimeを解放し、終了失敗時は後続呼び出しで再試行する]
    #   戻り値: [bool: frame producerが期限内に停止した場合はTrue]
    #   エラー: [WGC sessionまたはNative Runtimeの終了失敗: 次回closeで解放を再試行できる状態を保って送出]
    # }
    def close(self) -> bool:
        with self._lock:
            if self._closed:
                stopped = self._producer is None or self._stop_frame_producer()
            else:
                self._closed = True
                stopped = self._stop_frame_producer()
            if stopped and self._windows_capture is not None:
                self._windows_capture.close()
            return stopped

    # {
    #   責務: [_create_frame_analyzer: OCR recognizerを持つFrameAnalyzerを遅延importで作る]
    #   処理: [capture依存の読込を画面分析の実行時まで遅らせる]
    #   引数: [ocr_recognizer: frame analyzerへ注入するoptional OCR recognizer]
    #   戻り値: [FrameAnalyzer: frameをScreenObservationへ変換するanalyzer]
    # }
    @staticmethod
    def _create_frame_analyzer(ocr_recognizer: object | None) -> FrameAnalyzer:
        from ai_game_player.frame_analyzer import FrameAnalyzer

        return FrameAnalyzer(ocr_recognizer)

    # {
    #   責務: [_create_ocr_recognizer: 利用可能なTesseract OCR recognizerをoptionalに生成する]
    #   処理: [Tesseractが未導入または未設定でもframe analysisを続けられる認識器を要求する]
    #   引数: []
    #   戻り値: [object | None: 利用可能なOCR recognizer、未利用ならNone]
    # }
    @staticmethod
    def _create_ocr_recognizer() -> object | None:
        from ai_game_player.ocr_recognizer import TesseractOcrRecognizer

        return TesseractOcrRecognizer.optional()
