from collections import deque

from ai_game_player.models import ActionCandidate
from ai_game_player.screen_capture import ScreenFrame


class BrightRegionDetector:
    """Finds bright connected regions as low-confidence, bounded click candidates."""

    def __init__(self, brightness_threshold: int = 220, min_pixels: int = 9) -> None:
        if not 0 <= brightness_threshold <= 255:
            raise ValueError("brightness threshold must be between 0 and 255")
        if min_pixels < 1:
            raise ValueError("minimum pixels must be positive")
        self.brightness_threshold = brightness_threshold
        self.min_pixels = min_pixels

    def detect(self, frame: ScreenFrame) -> list[ActionCandidate]:
        visited = bytearray(frame.width * frame.height)
        candidates: list[ActionCandidate] = []
        for index in range(frame.width * frame.height):
            if visited[index] or not self._is_bright(frame, index):
                continue
            component = self._component(frame, index, visited)
            candidate = self._candidate(component, frame.width, len(candidates))
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _component(self, frame: ScreenFrame, start: int, visited: bytearray) -> list[int]:
        pending = deque([start])
        visited[start] = 1
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
                    visited[neighbor] = 1
                    pending.append(neighbor)
        return component

    def _candidate(self, component: list[int], width: int, index: int) -> ActionCandidate | None:
        if len(component) < self.min_pixels:
            return None
        xs = [pixel % width for pixel in component]
        ys = [pixel // width for pixel in component]
        left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
        region_width, region_height = right - left + 1, bottom - top + 1
        if region_width < 3 or region_height < 3:
            return None
        density = len(component) / (region_width * region_height)
        confidence = round(0.5 + 0.2 * density, 2)
        return ActionCandidate(
            f"bright-region-{index}",
            "click",
            "bright_region",
            left + region_width // 2,
            top + region_height // 2,
            confidence,
        )

    def _is_bright(self, frame: ScreenFrame, index: int) -> bool:
        offset = index * 4
        blue, green, red = frame.bgra[offset : offset + 3]
        brightness = (red * 299 + green * 587 + blue * 114) // 1000
        return brightness >= self.brightness_threshold