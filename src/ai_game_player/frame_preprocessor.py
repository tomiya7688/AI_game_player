"""Batch contracts and the dependency-free Python frame preprocessing path."""

from dataclasses import dataclass
from typing import Protocol

from ai_game_player.bright_region_detector import BrightRegionDetector
from ai_game_player.models import DetectedElement
from ai_game_player.perceptual_hasher import PerceptualHasher
from ai_game_player.screen_capture import ScreenFrame


@dataclass(frozen=True)
class FramePrimitiveBatch:
    mean_rgb: dict[str, int]
    mean_brightness: int
    perceptual_hash: str
    detected_elements: tuple[DetectedElement, ...]
    processing_ns: int | None = None
    input_bytes_read: int | None = None
    output_bytes_written: int | None = None
    input_copy_count: int | None = None
    input_copy_bytes: int | None = None


class FramePreprocessor(Protocol):
    def preprocess(
        self,
        frame: ScreenFrame,
        *,
        brightness_threshold: int = 220,
        min_region_pixels: int = 9,
    ) -> FramePrimitiveBatch: ...


class PythonFramePreprocessor:
    """Reference fallback whose outputs define the native implementation's parity."""

    def __init__(
        self,
        perceptual_hasher: PerceptualHasher | None = None,
        bright_region_detector: BrightRegionDetector | None = None,
    ) -> None:
        self.perceptual_hasher = perceptual_hasher or PerceptualHasher()
        self.bright_region_detector = bright_region_detector or BrightRegionDetector()

    def preprocess(
        self,
        frame: ScreenFrame,
        *,
        brightness_threshold: int = 220,
        min_region_pixels: int = 9,
    ) -> FramePrimitiveBatch:
        if frame.width <= 0 or frame.height <= 0 or len(frame.bgra) != frame.width * frame.height * 4:
            raise ValueError("BGRA buffer size does not match frame dimensions")
        pixels = frame.width * frame.height
        blue = sum(frame.bgra[0::4])
        green = sum(frame.bgra[1::4])
        red = sum(frame.bgra[2::4])
        detector = self.bright_region_detector
        if (
            detector.brightness_threshold != brightness_threshold
            or detector.min_pixels != min_region_pixels
        ):
            detector = BrightRegionDetector(brightness_threshold, min_region_pixels)
        return FramePrimitiveBatch(
            mean_rgb={"r": round(red / pixels), "g": round(green / pixels), "b": round(blue / pixels)},
            mean_brightness=round((red + green + blue) / (3 * pixels)),
            perceptual_hash=self.perceptual_hasher.hash(frame),
            detected_elements=tuple(detector.detect(frame)),
        )
