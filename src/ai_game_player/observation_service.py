from collections.abc import Callable
from typing import Protocol

from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture


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
#   責務: [LiveObservationService: 選択画面をcaptureしてObservationへ変換する]
#   フィールド: [capture_frame: 対象windowからframeを取得する関数, analyzer_factory: frameを解析する依存の生成関数, ocr_factory: optional OCR recognizer生成関数]
# }
class LiveObservationService:
    # {
    #   責務: [__init__: OS capture・frame analyzer・OCR recognizerの生成依存を受け取る]
    #   処理: [未指定依存だけをWindows向け実装で補い、テストではfakeへ差替え可能にする]
    #   引数: [capture_frame: window handleからframeを取得する関数, analyzer_factory: OCR依存からanalyzerを作る関数, ocr_factory: optional OCR recognizerを作る関数]
    #   戻り値: []
    # }
    def __init__(
        self,
        capture_frame: Callable[[int | None], ScreenFrame] | None = None,
        analyzer_factory: Callable[[object | None], FrameAnalyzer] | None = None,
        ocr_factory: Callable[[], object | None] | None = None,
    ) -> None:
        self.capture_frame = (
            capture_frame if capture_frame is not None else self._capture_windows_frame
        )
        self.analyzer_factory = (
            analyzer_factory
            if analyzer_factory is not None
            else self._create_frame_analyzer
        )
        self.ocr_factory = ocr_factory if ocr_factory is not None else self._create_ocr_recognizer

    # {
    #   責務: [capture_observation: 選択windowのcapture frameをObservationへ変換する]
    #   処理: [window handle付きcaptureを1回実行し、同じframeをlive screen IDで解析する]
    #   引数: [window_handle: capture対象のWindows window handle、省略時はcapture backendのdesktop動作]
    #   戻り値: [ScreenObservation: frameから生成した画面状態]
    #   エラー: [capture・OCR・frame解析で失敗した例外: 呼び出し元がUI表示またはloop停止を判断できるよう送出]
    # }
    def capture_observation(self, window_handle: int | None) -> ScreenObservation:
        frame = self.capture_frame(window_handle)
        analyzer = self.analyzer_factory(self.ocr_factory())
        return analyzer.analyze(frame, "live")

    # {
    #   責務: [_capture_windows_frame: Windows capture backendで画面frameを1枚取得する]
    #   処理: [選択window handleをWindowsScreenCaptureへ渡してframeを返す]
    #   引数: [window_handle: capture対象のWindows window、省略時はdesktop]
    #   戻り値: [ScreenFrame: capture backendが返したBGRA画面]
    # }
    @staticmethod
    def _capture_windows_frame(window_handle: int | None) -> ScreenFrame:
        return WindowsScreenCapture().capture(window_handle)

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
