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
BRIGHT_REGION_CONFIDENCE_BASE = 0.5
BRIGHT_REGION_DENSITY_WEIGHT = 0.2
BRIGHT_REGION_CONFIDENCE_DECIMAL_PLACES = 2
VISITED_PIXEL_MARKER = 1


class BrightRegionDetector:
    """Finds bright connected regions as low-confidence detected UI elements."""

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

    def _element(self, component: list[int], width: int, index: int) -> DetectedElement | None:
        if len(component) < self.min_pixels:
            return None
        xs = [pixel % width for pixel in component]
        ys = [pixel // width for pixel in component]
        left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
        region_width, region_height = right - left + 1, bottom - top + 1
        if region_width < MIN_BRIGHT_REGION_EXTENT or region_height < MIN_BRIGHT_REGION_EXTENT:
            return None
        density = len(component) / (region_width * region_height)
        confidence = round(
            BRIGHT_REGION_CONFIDENCE_BASE + BRIGHT_REGION_DENSITY_WEIGHT * density,
            BRIGHT_REGION_CONFIDENCE_DECIMAL_PLACES,
        )
        return DetectedElement(
            f"bright-region-{index}",
            "region",
            (left, top, region_width, region_height),
            "bright_region",
            confidence,
        )

    def _is_bright(self, frame: ScreenFrame, index: int) -> bool:
        offset = index * BGRA_BYTES_PER_PIXEL
        blue, green, red = frame.bgra[offset : offset + RGB_CHANNEL_COUNT]
        brightness = (
            red * RED_LUMA_WEIGHT + green * GREEN_LUMA_WEIGHT + blue * BLUE_LUMA_WEIGHT
        ) // LUMA_WEIGHT_TOTAL
        return brightness >= self.brightness_threshold
