from dataclasses import replace
from hashlib import sha256
from typing import Any

from ai_game_player.bright_region_detector import BGRA_BYTES_PER_PIXEL, BrightRegionDetector
from ai_game_player.capture_geometry import CaptureGeometry
from ai_game_player.capture_region import PixelRect
from ai_game_player.frame_preprocessor import FramePreprocessor, PythonFramePreprocessor
from ai_game_player.models import DetectedElement, ScreenObservation
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

    # {
    #   責務: [analyze: ROI pixelだけを画像/OCR解析し、操作候補とObservationをwindowまたはscreen座標へ戻す]
    #   引数: [frame: packed BGRAと任意のcapture geometryを持つ画面, screen_id: Observation識別名]
    #   戻り値: [ScreenObservation: ROI由来のfeaturesと、geometryがある場合は全座標系寸法を持つ状態]
    #   エラー: [BGRA寸法不一致またはanalyzer/OCR失敗: 部分Observationを返さず原因を送出]
    # }
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
        geometry = frame.capture_geometry
        detected_elements = _transform_detected_elements(primitive_batch.detected_elements, geometry)
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
            features["ocr_candidates"] = _transform_candidate_dicts(batch.to_candidate_dicts(), geometry)
            features["ocr_results"] = _transform_candidate_dicts(
                [result.to_dict() for result in batch.results], geometry
            )
            features["ocr_provider_status"] = [status.to_dict() for status in batch.statuses]
            features["ocr_fallback_used"] = batch.fallback_used
            detected_elements.extend(_transform_detected_elements(batch.elements, geometry))
        elif self.ocr is not None:
            features["ocr_candidates"] = _transform_candidate_dicts(self.ocr.recognize(frame), geometry)
        features["detected_elements"] = [element.to_dict() for element in detected_elements]
        if geometry is not None:
            features["capture_region"] = geometry.to_dict()
        ocr_text = [str(item["text"]) for item in features.get("ocr_candidates", [])]
        width, height = geometry.coordinate_size if geometry is not None else (frame.width, frame.height)
        return ScreenObservation(screen_id, width, height, ocr_text, features)


# {
#   責務: [_transform_detected_elements: ROI-local検出矩形を選択windowまたはscreenの候補座標へ移す]
#   引数: [elements: ROI内の検出結果, geometry: cropからscreen/windowへの変換。Noneなら入力値を保つ]
#   戻り値: [list[DetectedElement]: 入力座標系へ変換した検出要素]
# }
def _transform_detected_elements(
    elements: list[DetectedElement] | tuple[DetectedElement, ...],
    geometry: CaptureGeometry | None,
) -> list[DetectedElement]:
    if geometry is None:
        return list(elements)
    transformed_elements = []
    for element in elements:
        transformed_box = geometry.roi_rect_to_coordinates(PixelRect(*element.bbox))
        transformed_elements.append(replace(element, bbox=(
            transformed_box.x,
            transformed_box.y,
            transformed_box.width,
            transformed_box.height,
        )))
    return transformed_elements


# {
#   責務: [_transform_candidate_dicts: OCR候補とOCR結果の矩形・中心座標をROI外のscreen/window座標へ写す]
#   引数: [candidates: x/y/bboxまたはx/y/width/heightを持つOCR辞書列, geometry: capture座標変換。Noneなら入力辞書をcopyする]
#   戻り値: [list[dict[str, Any]]: 元辞書を変更せず変換した候補列]
# }
def _transform_candidate_dicts(
    candidates: list[dict[str, Any]],
    geometry: CaptureGeometry | None,
) -> list[dict[str, Any]]:
    if geometry is None:
        return [dict(candidate) for candidate in candidates]
    transformed_candidates = []
    for candidate in candidates:
        transformed = dict(candidate)
        raw_box = candidate.get("bbox")
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            source_box = PixelRect(*(int(value) for value in raw_box))
        elif all(key in candidate for key in ("x", "y", "width", "height")):
            source_box = PixelRect(
                int(candidate["x"]),
                int(candidate["y"]),
                int(candidate["width"]),
                int(candidate["height"]),
            )
        else:
            source_box = None
        if source_box is not None:
            output_box = geometry.roi_rect_to_coordinates(source_box)
            transformed["bbox"] = (
                output_box.x,
                output_box.y,
                output_box.width,
                output_box.height,
            )
            if "width" in candidate and "height" in candidate:
                transformed["x"] = output_box.x
                transformed["y"] = output_box.y
                transformed["width"] = output_box.width
                transformed["height"] = output_box.height
            elif "x" in candidate and "y" in candidate:
                transformed["x"] = output_box.x + output_box.width // 2
                transformed["y"] = output_box.y + output_box.height // 2
        elif "x" in candidate and "y" in candidate:
            x_value = float(candidate["x"])
            y_value = float(candidate["y"])
            if geometry.window_bounds is not None:
                output_x, output_y = geometry.roi_to_window(x_value, y_value)
            else:
                output_x, output_y = geometry.roi_to_screen(x_value, y_value)
            transformed["x"] = round(output_x)
            transformed["y"] = round(output_y)
        transformed_candidates.append(transformed)
    return transformed_candidates
