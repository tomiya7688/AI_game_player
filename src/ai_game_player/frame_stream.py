"""Contracts and immutable values shared by capture-stream consumers."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ai_game_player.screen_capture import ScreenFrame


# {
#   責務: [FrameCapture: 1枚の画面Frameを取得するcapture backend契約を定義する]
#   処理: [capture: 次のScreenFrameを同期取得する]
#   戻り値: [ScreenFrame: BGRA pixelとmonotonic取得時刻を持つ画面]
# }
class FrameCapture(Protocol):
    # {
    #   責務: [capture: 選択中のcapture sourceから1枚取得する]
    #   戻り値: [ScreenFrame: 取得した画面と取得時刻]
    # }
    def capture(self) -> ScreenFrame: ...


# {
#   責務: [FrameDeliveryMode: consumerごとのbounded queue配送方針を表す]
#   値: [LATEST: 未読frameを最新frameへ置き換える, ORDERED: 既存queueを保ち満杯時の新規frameを破棄する]
# }
class FrameDeliveryMode(Enum):
    LATEST = "latest"
    ORDERED = "ordered"


# {
#   責務: [FramePacket: producerがframeへ付与する順序IDとsource世代を保持する]
#   フィールド: [frame_id: producer内で単調増加するcapture ID, source_generation: source切替ごとに増加する世代, frame: 取得時刻とpixelを持つScreenFrame]
# }
@dataclass(frozen=True)
class FramePacket:
    frame_id: int
    source_generation: int
    frame: ScreenFrame


# {
#   責務: [FrameProducerMetrics: producerのcapture・配送・破棄とlifecycle状態を読み取り専用で示す]
#   フィールド: [captured_frames: backendが成功して返したframe数, published_frames: subscriber queueへ追加したpacket数, dropped_frames: source切替・停止要求・queue満杯で破棄した配送数, source_generation: 現在のsource世代, last_frame_id: 最後に取得したframe ID, is_running: capture threadの実行状態, error_message: capture失敗時の型名と説明]
# }
@dataclass(frozen=True)
class FrameProducerMetrics:
    captured_frames: int
    published_frames: int
    dropped_frames: int
    source_generation: int
    last_frame_id: int
    is_running: bool
    error_message: str | None


# {
#   責務: [FrameProducerError: capture backendの失敗をconsumerへ通知する]
#   処理: [元のbackend例外をcauseとして保持し、capture停止の理由を呼び出し側へ伝える]
# }
class FrameProducerError(RuntimeError):
    pass
