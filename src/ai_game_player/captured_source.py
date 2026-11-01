from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.frame_stream import FrameCapture
from dataclasses import replace
from uuid import uuid4


# {
#   責務: [CapturedObservationSource: 一つの実capture sourceから認識結果と新Frame参照を生成する]
#   フィールド: [capture_device/analyzer: 実取得と認識, screen_id: 対象表示,
#     _source_id/_sequence: このcapture instance内のscopeと取得順序]
# }
class CapturedObservationSource:
    # {
    #   責務: [__init__: 実capture deviceとsource識別子を固定する]
    #   処理: [Frameを捏造せず取得順序0で開始する]
    #   引数: [capture: 取得元, screen_id: 観測対象名] 戻り値: [なし]
    # }
    def __init__(self, capture: FrameCapture, screen_id: str = "screen") -> None:
        self.capture_device = capture
        self.analyzer = FrameAnalyzer()
        self.screen_id = screen_id
        self._source_id = "capture." + uuid4().hex
        self._sequence = 0

    # {
    #   責務: [read: 実Frameを一回取得し同じ画素から観測を作る]
    #   処理: [元capture時刻とsource識別をmetadataへ残す]
    #   引数: [なし] 戻り値: [tuple: 観測と設定候補無しの一覧]
    # }
    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        frame = self.capture_device.capture()
        observation = self.analyzer.analyze(frame, self.screen_id)
        self._sequence += 1
        return replace(
            observation,
            features={
                **observation.features,
                "capture_metadata": {
                    "source_id": self._source_id,
                    "frame_id": f"{self._source_id}:0:{self._sequence}",
                    "source_generation": 0,
                    "monotonic_ns": round(frame.captured_at * 1_000_000_000),
                },
            },
        ), []

    # {
    #   責務: [read_fresh: cached観測を反復せず同じcapture deviceから実Frameを再取得する]
    #   処理: [通常のsensor取得を一度だけ呼ぶ] 引数: [なし]
    #   戻り値: [tuple: 新しい観測と候補。取得時刻も進まなければ利用側が拒否する]
    # }
    def read_fresh(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        return self.read()
