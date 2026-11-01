import unittest
from ai_game_player.ocr_recognizer import TesseractOcrRecognizer
from ai_game_player.screen_capture import ScreenFrame


class FakeTesseract:
    class Output:
        DICT = "dict"

    def image_to_data(self, image, output_type=None):
        return {
            "text": ["START", ""],
            "conf": ["90", "-1"],
            "left": [2, 0],
            "top": [3, 0],
            "width": [20, 0],
            "height": [8, 0],
        }


class FakeImage:
    @staticmethod
    def frombytes(*args):
        return object()


class OcrRecognizerTest(unittest.TestCase):
    def test_numeric_line_profile_is_explicit_and_document_default_is_preserved(self):
        calls = []

        class NumericTesseract(FakeTesseract):
            def image_to_data(self, image, output_type=None, **kwargs):
                calls.append(kwargs)
                return super().image_to_data(image, output_type)

        ocr = TesseractOcrRecognizer((NumericTesseract(), FakeImage))
        frame = ScreenFrame(30, 20, b"x" * 30 * 20 * 4)
        ocr.recognize(frame)
        ocr.recognize(frame, mode="single_numeric_line")
        ocr.recognize(frame, mode="text_block")
        self.assertEqual(
            calls,
            [
                {},
                {"config": "--psm 7 -c tessedit_char_whitelist=0123456789,"},
                {"config": "--psm 6"},
            ],
        )
        with self.assertRaises(ValueError):
            ocr.recognize(frame, mode="unknown")

    def test_converts_tesseract_data_to_candidates(self):
        result = TesseractOcrRecognizer((FakeTesseract(), FakeImage)).recognize(
            ScreenFrame(30, 20, b"x" * 30 * 20 * 4)
        )
        self.assertEqual(result[0]["text"], "START")
        self.assertEqual(result[0]["x"], 2)
