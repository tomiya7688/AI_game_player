from hashlib import sha256

from ai_game_player.bright_region_detector import BGRA_BYTES_PER_PIXEL, BrightRegionDetector
from ai_game_player.frame_preprocessor import FramePreprocessor, PythonFramePreprocessor
from ai_game_player.models import ScreenObservation
from ai_game_player.perceptual_hasher import PerceptualHasher
from ai_game_player.screen_capture import ScreenFrame


class FrameAnalyzer:
    """Converts a raw frame into stable visual features and optional OCR."""

    def __init__(
        self,
        ocr=None,
        perceptual_hasher: PerceptualHasher | None = None,
        bright_region_detector: BrightRegionDetector | None = None,
        frame_preprocessor: FramePreprocessor | None = None,
    ) -> None:
        self.ocr = ocr
        self.perceptual_hasher = perceptual_hasher or PerceptualHasher()
        self.bright_region_detector = bright_region_detector or BrightRegionDetector()
        self.frame_preprocessor = frame_preprocessor if frame_preprocessor is not None else PythonFramePreprocessor(
            self.perceptual_hasher,
            self.bright_region_detector,
        )

    def analyze(self, frame: ScreenFrame, screen_id: str = "screen") -> ScreenObservation:
        if len(frame.bgra) != frame.width * frame.height * BGRA_BYTES_PER_PIXEL:
            raise ValueError("BGRA buffer size does not match frame dimensions")
        primitive_batch = self.frame_preprocessor.preprocess(
            frame,
            brightness_threshold=self.bright_region_detector.brightness_threshold,
            min_region_pixels=self.bright_region_detector.min_pixels,
        )
        features = {
            "mean_rgb": primitive_batch.mean_rgb,
            "mean_brightness": primitive_batch.mean_brightness,
            "signature": sha256(frame.bgra).hexdigest(),
            "perceptual_hash": primitive_batch.perceptual_hash,
        }
        detected_elements = list(primitive_batch.detected_elements)
        features["image_candidates"] = [
            {
                "action_id": element.element_id,
                "kind": element.kind,
                "label": element.text or element.element_type,
                "x": element.bbox[0] + element.bbox[2] // 2,
                "y": element.bbox[1] + element.bbox[3] // 2,
                "confidence": element.confidence,
                "dangerous": element.dangerous,
                "bbox": element.bbox,
            }
            for element in detected_elements
        ]
        if self.ocr is not None and hasattr(self.ocr, "recognize_batch"):
            batch = self.ocr.recognize_batch(frame, screen_id)
            features["ocr_candidates"] = batch.to_candidate_dicts()
            features["ocr_results"] = [result.to_dict() for result in batch.results]
            features["ocr_provider_status"] = [status.to_dict() for status in batch.statuses]
            features["ocr_fallback_used"] = batch.fallback_used
            detected_elements.extend(batch.elements)
        elif self.ocr is not None:
            features["ocr_candidates"] = self.ocr.recognize(frame)
        features["detected_elements"] = [element.to_dict() for element in detected_elements]
        ocr_text = [str(item["text"]) for item in features.get("ocr_candidates", [])]
        return ScreenObservation(screen_id, frame.width, frame.height, ocr_text, features)
