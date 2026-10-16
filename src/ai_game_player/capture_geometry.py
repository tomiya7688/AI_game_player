from dataclasses import dataclass, replace
from math import ceil, floor, isfinite
from typing import Any

from ai_game_player.capture_region import CaptureRegion, CaptureRegionMode, PixelRect


# {
#   責務: [BGRA_BYTES_PER_PIXEL: packed BGRA frameの1pixelを構成するbyte数を定義する]
#   値: [4: blue/green/red/alpha各1byte]
# }
BGRA_BYTES_PER_PIXEL = 4


# {
#   責務: [CaptureGeometry: crop-local pixel、screen pixel、window pixel、正規化座標の変換と取得根拠を保持する]
#   フィールド: [mode: capture方式, crop_rect/source_width/source_height: 元frame内ROIと倍率, screen_bounds/window_bounds: screen/window位置, confidence/source: 領域の確信度と出所]
# }
@dataclass(frozen=True)
class CaptureGeometry:
    mode: CaptureRegionMode
    crop_rect: PixelRect
    source_width: int
    source_height: int
    screen_bounds: PixelRect
    window_bounds: PixelRect | None
    confidence: float
    source: str

    # {
    #   責務: [__post_init__: capture geometryに有限な確信度と非空な出所が含まれることを検証する]
    #   戻り値: []
    #   エラー: [confidenceが0から1の範囲外、sourceが空: 誤った変換情報を利用者へ公開しない]
    # }
    def __post_init__(self) -> None:
        if self.source_width <= 0 or self.source_height <= 0:
            raise ValueError("capture geometry source dimensions must be positive")
        source_frame = PixelRect(0, 0, self.source_width, self.source_height)
        if not source_frame.contains(self.crop_rect):
            raise ValueError("capture geometry crop must stay inside its source frame")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("capture geometry confidence must be between 0 and 1")
        if not self.source.strip():
            raise ValueError("capture geometry source must not be empty")

    # {
    #   責務: [coordinate_size: 候補を返すwindow座標系またはdesktop座標系の全体寸法を返す]
    #   戻り値: [tuple[int, int]: window選択時はwindow寸法、それ以外はscreen寸法]
    # }
    @property
    def coordinate_size(self) -> tuple[int, int]:
        bounds = self.window_bounds or self.screen_bounds
        return bounds.width, bounds.height

    # {
    #   責務: [roi_to_screen: crop-local pixel点を絶対screen座標へ線形変換する]
    #   引数: [x/y: ROI左上を原点とするpixel座標]
    #   戻り値: [tuple[float, float]: desktop上のscreen pixel座標]
    # }
    def roi_to_screen(self, x: float, y: float) -> tuple[float, float]:
        source_x = self.crop_rect.x + x
        source_y = self.crop_rect.y + y
        return (
            self.screen_bounds.x + source_x * self.screen_bounds.width / self.source_width,
            self.screen_bounds.y + source_y * self.screen_bounds.height / self.source_height,
        )

    # {
    #   責務: [screen_to_roi: absolute screen pixel点をcrop-local pixel座標へ逆変換する]
    #   引数: [x/y: desktop上のscreen pixel座標]
    #   戻り値: [tuple[float, float]: ROI左上を原点とするpixel座標]
    # }
    def screen_to_roi(self, x: float, y: float) -> tuple[float, float]:
        return (
            (x - self.screen_bounds.x) * self.source_width / self.screen_bounds.width - self.crop_rect.x,
            (y - self.screen_bounds.y) * self.source_height / self.screen_bounds.height - self.crop_rect.y,
        )

    # {
    #   責務: [roi_to_window: crop-local pixel点を選択window外枠内のpixel座標へ変換する]
    #   引数: [x/y: ROI左上を原点とするpixel座標]
    #   戻り値: [tuple[float, float]: window左上を原点とするpixel座標]
    #   エラー: [window_boundsがない: window座標変換の対象が特定できない]
    # }
    def roi_to_window(self, x: float, y: float) -> tuple[float, float]:
        if self.window_bounds is None:
            raise ValueError("window coordinate conversion requires selected-window bounds")
        screen_x, screen_y = self.roi_to_screen(x, y)
        return screen_x - self.window_bounds.x, screen_y - self.window_bounds.y

    # {
    #   責務: [window_to_roi: 選択window左上原点のpixel点をcrop-local座標へ逆変換する]
    #   引数: [x/y: window左上を原点とするpixel座標]
    #   戻り値: [tuple[float, float]: ROI左上を原点とするpixel座標]
    #   エラー: [window_boundsがない: window座標変換の対象が特定できない]
    # }
    def window_to_roi(self, x: float, y: float) -> tuple[float, float]:
        if self.window_bounds is None:
            raise ValueError("window coordinate conversion requires selected-window bounds")
        return self.screen_to_roi(x + self.window_bounds.x, y + self.window_bounds.y)

    # {
    #   責務: [roi_to_normalized: crop-local pixel点をwindowまたはscreenの0から1までの比率へ変換する]
    #   引数: [x/y: ROI左上を原点とするpixel座標]
    #   戻り値: [tuple[float, float]: 全座標系寸法に対するx/y比率]
    # }
    def roi_to_normalized(self, x: float, y: float) -> tuple[float, float]:
        if self.window_bounds is not None:
            coordinate_x, coordinate_y = self.roi_to_window(x, y)
        else:
            screen_x, screen_y = self.roi_to_screen(x, y)
            coordinate_x = screen_x - self.screen_bounds.x
            coordinate_y = screen_y - self.screen_bounds.y
        width, height = self.coordinate_size
        return coordinate_x / width, coordinate_y / height

    # {
    #   責務: [normalized_to_roi: windowまたはscreen内の0から1までの比率点をcrop-local pixelへ逆変換する]
    #   引数: [x/y: 全座標系寸法に対する0から1までの比率]
    #   戻り値: [tuple[float, float]: ROI左上を原点とするpixel座標]
    #   エラー: [比率が有限値でない、0から1の外: 存在しない画面位置を拒否]
    # }
    def normalized_to_roi(self, x: float, y: float) -> tuple[float, float]:
        if not isfinite(x) or not isfinite(y) or not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError("normalized coordinates must be finite values between 0 and 1")
        width, height = self.coordinate_size
        coordinate_x, coordinate_y = x * width, y * height
        if self.window_bounds is not None:
            return self.window_to_roi(coordinate_x, coordinate_y)
        return self.screen_to_roi(
            coordinate_x + self.screen_bounds.x,
            coordinate_y + self.screen_bounds.y,
        )

    # {
    #   責務: [roi_rect_to_coordinates: ROI-local bounding boxをwindowまたはscreen座標の最小包含boxへ変換する]
    #   引数: [rect: ROI左上を原点とする正のpixel bounding box]
    #   戻り値: [PixelRect: input candidateで利用する座標系のbounding box]
    # }
    def roi_rect_to_coordinates(self, rect: PixelRect) -> PixelRect:
        convert = self.roi_to_window if self.window_bounds is not None else self.roi_to_screen
        corners = (
            convert(rect.x, rect.y),
            convert(rect.right, rect.y),
            convert(rect.x, rect.bottom),
            convert(rect.right, rect.bottom),
        )
        left = floor(min(point[0] for point in corners))
        top = floor(min(point[1] for point in corners))
        right = ceil(max(point[0] for point in corners))
        bottom = ceil(max(point[1] for point in corners))
        return PixelRect(left, top, max(1, right - left), max(1, bottom - top))

    # {
    #   責務: [to_dict: 現在frameのROI、screen/window bounds、座標系、信頼度、出所を記録する]
    #   戻り値: [dict[str, object]: capture結果をJSONへ保存できるgeometry metadata]
    # }
    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "mode": self.mode.value,
            "source": self.source,
            "confidence": self.confidence,
            "coordinate_space": "window" if self.window_bounds is not None else "screen",
            "crop_rect": self.crop_rect.to_dict(),
            "screen_roi": self.roi_rect_to_screen().to_dict(),
            "screen_bounds": self.screen_bounds.to_dict(),
            "source_frame_size": {"width": self.source_width, "height": self.source_height},
            "coordinate_size": {"width": self.coordinate_size[0], "height": self.coordinate_size[1]},
        }
        if self.window_bounds is not None:
            value["window_bounds"] = self.window_bounds.to_dict()
        return value

    # {
    #   責務: [roi_rect_to_screen: ROI全体をscreen座標の整数矩形へ外側に丸めて返す]
    #   戻り値: [PixelRect: desktop上のscreen pixel ROI]
    # }
    def roi_rect_to_screen(self) -> PixelRect:
        corners = (
            self.roi_to_screen(0, 0),
            self.roi_to_screen(self.crop_rect.width, 0),
            self.roi_to_screen(0, self.crop_rect.height),
            self.roi_to_screen(self.crop_rect.width, self.crop_rect.height),
        )
        left = floor(min(point[0] for point in corners))
        top = floor(min(point[1] for point in corners))
        right = ceil(max(point[0] for point in corners))
        bottom = ceil(max(point[1] for point in corners))
        return PixelRect(left, top, max(1, right - left), max(1, bottom - top))


# {
#   責務: [crop_screen_frame: BGRA frameから解決済みROIだけをコピーし、座標変換metadata付きframeを返す]
#   引数: [frame: raw BGRA bytesとscreen/window boundsを持つ画面frame, region: frameを切り出すCaptureRegion]
#   戻り値: [ScreenFrame: ROI内のpixelだけを含み、元screen/windowへ戻すgeometryを保持するframe]
#   エラー: [BGRA byte数不一致、fixed ROIがdesktop外、window-relative modeにwindow boundsなし: 部分frameを返さず失敗]
# }
def crop_screen_frame(frame: Any, region: CaptureRegion) -> Any:
    bytes_per_pixel = BGRA_BYTES_PER_PIXEL
    if len(frame.bgra) != frame.width * frame.height * bytes_per_pixel:
        raise ValueError("BGRA buffer size does not match frame dimensions")
    screen_bounds = frame.screen_bounds or PixelRect(0, 0, frame.width, frame.height)
    geometry = region.resolve(frame.width, frame.height, screen_bounds, frame.window_bounds)
    crop = geometry.crop_rect
    if crop == PixelRect(0, 0, frame.width, frame.height):
        cropped_bgra = frame.bgra
    else:
        row_size = crop.width * bytes_per_pixel
        source_pixels = memoryview(frame.bgra)
        cropped_pixels = bytearray(row_size * crop.height)
        for row in range(crop.height):
            source_start = ((crop.y + row) * frame.width + crop.x) * bytes_per_pixel
            target_start = row * row_size
            cropped_pixels[target_start : target_start + row_size] = source_pixels[source_start : source_start + row_size]
        cropped_bgra = bytes(cropped_pixels)
    return replace(
        frame,
        width=crop.width,
        height=crop.height,
        bgra=cropped_bgra,
        capture_geometry=geometry,
    )
