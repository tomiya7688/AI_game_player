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

    # {
    #   責務: [recognize: 同じ画素からOCR文字と矩形・確信度を返す]
    #   処理: [既定は文書解析。明示的な局所数字行だけPSM7と数字語彙を使う]
    #   引数: [frame: 解析画素, mode: documentまたはsingle_numeric_line]
    #   戻り値: [list: 元frame内のOCR候補] エラー: [ValueError: 未定義mode]
    # }
    def recognize(
        self, frame: ScreenFrame, *, mode: str = "document"
    ) -> list[dict[str, Any]]:
        if mode not in {"document", "single_numeric_line"}:
            raise ValueError("unsupported OCR segmentation mode")
        if self._tesseract is None:
            raise RuntimeError("OCR requires pytesseract and Pillow")
        pytesseract, image_module = self._tesseract
        image = image_module.frombytes(
            "RGB", (frame.width, frame.height), frame.bgra, "raw", "BGRX"
        )
        options = (
            {"config": "--psm 7 -c tessedit_char_whitelist=0123456789,"}
            if mode == "single_numeric_line"
            else {}
        )
        data = pytesseract.image_to_data(
            image, output_type=pytesseract.Output.DICT, **options
        )
        result = []
        for index, text in enumerate(data.get("text", [])):
            text = str(text).strip()
            confidence = (
                float(data["conf"][index]) / 100
                if str(data["conf"][index]).strip() not in {"", "-1"}
                else 0.0
            )
            if text and confidence > 0:
                result.append(
                    {
                        "text": text,
                        "x": int(data["left"][index]),
                        "y": int(data["top"][index]),
                        "width": int(data["width"][index]),
                        "height": int(data["height"][index]),
                        "confidence": confidence,
                    }
                )
        return result
