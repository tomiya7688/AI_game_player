"""Batch contracts and the dependency-free Python frame preprocessing path."""

from dataclasses import dataclass
from typing import Protocol

from ai_game_player.bright_region_detector import (
    BGRA_BYTES_PER_PIXEL,
    DEFAULT_BRIGHTNESS_THRESHOLD,
    DEFAULT_MIN_REGION_PIXELS,
    RGB_CHANNEL_COUNT,
    BrightRegionDetector,
)
from ai_game_player.models import DetectedElement
from ai_game_player.perceptual_hasher import PerceptualHasher
from ai_game_player.screen_capture import ScreenFrame


@dataclass(frozen=True)
class FramePrimitiveBatch:
    """Computed frame features and optional native-work metrics.

    Native payload bytes count logical BGRA bytes per ABI call, summed across
    capacity retries, and exclude row padding.
    """

    mean_rgb: dict[str, int]
    mean_brightness: int
    perceptual_hash: str
    detected_elements: tuple[DetectedElement, ...]
    processing_ns: int | None = None
    input_frame_bytes_processed: int | None = None
    output_bytes_written: int | None = None
    input_copy_count: int | None = None
    input_copy_bytes: int | None = None


class FramePreprocessor(Protocol):
    def preprocess(
        self,
        frame: ScreenFrame,
        *,
        brightness_threshold: int = DEFAULT_BRIGHTNESS_THRESHOLD,
        min_region_pixels: int = DEFAULT_MIN_REGION_PIXELS,
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
        brightness_threshold: int = DEFAULT_BRIGHTNESS_THRESHOLD,
        min_region_pixels: int = DEFAULT_MIN_REGION_PIXELS,
    ) -> FramePrimitiveBatch:
        if frame.width <= 0 or frame.height <= 0 or len(frame.bgra) != frame.width * frame.height * 4:
            raise ValueError("BGRA buffer size does not match frame dimensions")
        pixel_count = frame.width * frame.height
        blue_channel_sum = sum(frame.bgra[0::BGRA_BYTES_PER_PIXEL])
        green_channel_sum = sum(frame.bgra[1::BGRA_BYTES_PER_PIXEL])
        red_channel_sum = sum(frame.bgra[2::BGRA_BYTES_PER_PIXEL])
        active_detector = self.bright_region_detector
        if (
            active_detector.brightness_threshold != brightness_threshold
            or active_detector.min_pixels != min_region_pixels
        ):
            active_detector = BrightRegionDetector(brightness_threshold, min_region_pixels)
        return FramePrimitiveBatch(
            mean_rgb={
                "r": round(red_channel_sum / pixel_count),
                "g": round(green_channel_sum / pixel_count),
                "b": round(blue_channel_sum / pixel_count),
            },
            mean_brightness=round(
                (red_channel_sum + green_channel_sum + blue_channel_sum)
                / (RGB_CHANNEL_COUNT * pixel_count)
            ),
            perceptual_hash=self.perceptual_hasher.hash(frame),
            detected_elements=tuple(active_detector.detect(frame)),
        )
