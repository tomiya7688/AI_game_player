from collections import deque

from ai_game_player.models import DetectedElement
from ai_game_player.screen_capture import ScreenFrame

DEFAULT_BRIGHTNESS_THRESHOLD = 220
DEFAULT_MIN_REGION_PIXELS = 9
BGRA_BYTES_PER_PIXEL = 4
RGB_CHANNEL_COUNT = 3
MAX_CHANNEL_VALUE = 255
RED_LUMA_WEIGHT = 299
GREEN_LUMA_WEIGHT = 587
BLUE_LUMA_WEIGHT = 114
LUMA_WEIGHT_TOTAL = 1000
MIN_BRIGHT_REGION_EXTENT = 3
UNVERIFIED_BRIGHT_REGION_CONFIDENCE = 0.0
VISITED_PIXEL_MARKER = 1


# {
#   責務: [BrightRegionDetector: 輝度が高い連結領域を検出し、UI要素かどうかは未確認の要素として返す]
#   制約: [領域の明るさと密度は画素領域の検出にだけ使い、クリック可能性や実行信頼度の根拠には使わない]
# }
class BrightRegionDetector:
    """Return bright connected pixel regions without claiming that they are UI controls."""

    def __init__(
        self,
        brightness_threshold: int = DEFAULT_BRIGHTNESS_THRESHOLD,
        min_pixels: int = DEFAULT_MIN_REGION_PIXELS,
    ) -> None:
        if not 0 <= brightness_threshold <= MAX_CHANNEL_VALUE:
            raise ValueError("brightness threshold must be between 0 and 255")
        if min_pixels < 1:
            raise ValueError("minimum pixels must be positive")
        self.brightness_threshold = brightness_threshold
        self.min_pixels = min_pixels

    def detect(self, frame: ScreenFrame) -> list[DetectedElement]:
        visited = bytearray(frame.width * frame.height)
        elements: list[DetectedElement] = []
        for index in range(frame.width * frame.height):
            if visited[index] or not self._is_bright(frame, index):
                continue
            component = self._component(frame, index, visited)
            element = self._element(component, frame.width, len(elements))
            if element is not None:
                elements.append(element)
        return elements

    def _component(self, frame: ScreenFrame, start: int, visited: bytearray) -> list[int]:
        pending = deque([start])
        visited[start] = VISITED_PIXEL_MARKER
        component: list[int] = []
        while pending:
            index = pending.popleft()
            component.append(index)
            x, y = index % frame.width, index // frame.width
            for neighbor_x, neighbor_y in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if not (0 <= neighbor_x < frame.width and 0 <= neighbor_y < frame.height):
                    continue
                neighbor = neighbor_y * frame.width + neighbor_x
                if not visited[neighbor] and self._is_bright(frame, neighbor):
                    visited[neighbor] = VISITED_PIXEL_MARKER
                    pending.append(neighbor)
        return component

    # {
    #   責務: [_element: 有効な画素領域をUI未確認のDetectedElementへ変換する]
    #   処理: [最小画素数と矩形寸法を確認し、矩形と検出順を保持してUI確信度を0にする]
    #   引数: [component: 連結した明画素のframe内index列, width: frame幅, index: 検出順を示す番号]
    #   戻り値: [DetectedElement: 領域矩形と未確認confidenceを持つ要素。サイズ条件を満たさない場合はNone]
    # }
    def _element(self, component: list[int], width: int, index: int) -> DetectedElement | None:
        if len(component) < self.min_pixels:
            return None
        xs = [pixel % width for pixel in component]
        ys = [pixel // width for pixel in component]
        left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
        region_width, region_height = right - left + 1, bottom - top + 1
        if region_width < MIN_BRIGHT_REGION_EXTENT or region_height < MIN_BRIGHT_REGION_EXTENT:
            return None
        return DetectedElement(
            f"bright-region-{index}",
            "region",
            (left, top, region_width, region_height),
            "bright_region",
            UNVERIFIED_BRIGHT_REGION_CONFIDENCE,
        )

    def _is_bright(self, frame: ScreenFrame, index: int) -> bool:
        offset = index * BGRA_BYTES_PER_PIXEL
        blue, green, red = frame.bgra[offset : offset + RGB_CHANNEL_COUNT]
        brightness = (
            red * RED_LUMA_WEIGHT + green * GREEN_LUMA_WEIGHT + blue * BLUE_LUMA_WEIGHT
        ) // LUMA_WEIGHT_TOTAL
        return brightness >= self.brightness_threshold
