from typing import Any
from ai_game_player.screen_capture import ScreenFrame


class TesseractOcrRecognizer:
    """Optional OCR adapter; returns candidate-compatible text boxes."""

    def __init__(self, tesseract=None) -> None:
        self._tesseract = tesseract

    @classmethod
    def optional(cls) -> "TesseractOcrRecognizer | None":
        try:
            import pytesseract
            from PIL import Image
            return cls((pytesseract, Image))
        except ImportError:
            return None

    def recognize(self, frame: ScreenFrame) -> list[dict[str, Any]]:
        if self._tesseract is None:
            raise RuntimeError("OCR requires pytesseract and Pillow")
        pytesseract, image_module = self._tesseract
        image = image_module.frombytes("RGB", (frame.width, frame.height), frame.bgra, "raw", "BGRX")
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        result = []
        for index, text in enumerate(data.get("text", [])):
            text = str(text).strip()
            confidence = float(data["conf"][index]) / 100 if str(data["conf"][index]).strip() not in {"", "-1"} else 0.0
            if text and confidence > 0:
                result.append({"text": text, "x": int(data["left"][index]), "y": int(data["top"][index]), "width": int(data["width"][index]), "height": int(data["height"][index]), "confidence": confidence})
        return result