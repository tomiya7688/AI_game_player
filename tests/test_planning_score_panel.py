from dataclasses import replace
import unittest

from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.planning_score_evaluation import reread_score_line
from tools.planning_score_panel import (
    Panel,
    _otsu,
    find_panels,
    numeric_crops,
    prepare_panel_ocr,
)


def panel_frame(best=240):
    width, height = 200, 100
    raw = bytearray(b"\xfa\xfa\xfa\xff" * (width * height))
    for left, value in ((30, 180), (105, 150)):
        for y in range(20, 75):
            for x in range(left, left + 65):
                offset = (y * width + x) * 4
                raw[offset : offset + 4] = bytes((value, value, value, 255))
    for left, value in ((45, 245), (120, best)):
        for y in range(50, 65):
            for x in range(left, left + 25):
                offset = (y * width + x) * 4
                raw[offset : offset + 4] = bytes((value, value, value, 255))
    return ScreenFrame(width, height, bytes(raw), captured_at=19)


class ScorePanelTests(unittest.TestCase):
    def test_otsu_uses_histogram_separation_and_rejects_uniform(self):
        self.assertEqual(_otsu([20] * 20 + [200] * 10), 20)
        with self.assertRaises(ValueError):
            _otsu([20] * 30)

    def test_component_support_keeps_two_distinct_panels_not_crop_edges(self):
        frame = panel_frame()
        self.assertEqual(
            find_panels(frame, (0, 0, 200, 100)),
            [Panel(30, 20, 95, 75), Panel(105, 20, 170, 75)],
        )
        self.assertEqual(find_panels(frame, (30, 20, 95, 75)), [])
        with self.assertRaises(ValueError):
            find_panels(frame, (-1, 0, 100, 100))
        larger = ScreenFrame(400, 400, bytes(400 * 400 * 4), captured_at=1)
        with self.assertRaises(ValueError):
            find_panels(larger, (0, 0, 400, 400))

    def test_best_changes_cannot_change_score_preprocessing(self):
        panel = Panel(30, 20, 95, 75)
        prepared = []
        for best in (30, 220, 255):
            frame = panel_frame(best)
            before = frame.bgra
            result, padding = prepare_panel_ocr(frame, (20, 42, 110, 74), panel)
            prepared.append(result.bgra)
            self.assertEqual(frame.bgra, before)
            self.assertEqual(result.captured_at, 19)
            self.assertEqual(padding, 12)
            self.assertEqual(
                result.bgra[: result.width * 12 * 4], b"\xff" * (result.width * 12 * 4)
            )
        self.assertEqual(prepared[0], prepared[1])
        self.assertEqual(prepared[1], prepared[2])

    def observation(self):
        return ScreenObservation(
            "score",
            200,
            100,
            features={
                "ocr_candidates": [
                    {"text": "SCORE", "confidence": 0.95, "bbox": (45, 30, 25, 10)}
                ]
            },
        )

    def test_source_foreground_touching_any_strip_boundary_is_rejected_before_padding(
        self,
    ):
        frame = panel_frame()
        panel = Panel(30, 20, 95, 75)
        for box in (
            (45, 42, 80, 74),
            (32, 42, 70, 74),
            (32, 50, 93, 74),
            (32, 42, 93, 65),
        ):
            with (
                self.subTest(box=box),
                self.assertRaisesRegex(ValueError, "source support boundary"),
            ):
                prepare_panel_ocr(frame, box, panel, numeric=True)

    def test_clamping_two_foreground_margins_to_same_roi_stays_unknown(self):
        frame = panel_frame()
        raw = bytearray(frame.bgra)
        for y in range(50, 65):
            for x in range(33, 92):
                offset = (y * frame.width + x) * 4
                raw[offset : offset + 4] = b"\xf5\xf5\xf5\xff"
        with self.assertRaisesRegex(ValueError, "two distinct"):
            numeric_crops(
                replace(frame, bgra=bytes(raw)), Panel(30, 20, 95, 75), 40, 74
            )

    def test_white_border_does_not_hide_clipped_numeric_evidence(self):
        class ClippedOcr:
            def recognize(self, frame, *, mode):
                return [
                    {
                        "text": "1234",
                        "confidence": 0.99,
                        "x": 12,
                        "y": 24,
                        "width": 30,
                        "height": 20,
                    }
                ]

        result, diagnostic = reread_score_line(
            panel_frame(), self.observation(), ClippedOcr()
        )
        self.assertIsNone(result)
        self.assertEqual(diagnostic["status"], "score_line_touches_crop_edge")

    def test_two_width_disagreement_remains_unknown_after_preprocessing(self):
        class DisagreeingOcr:
            def __init__(self):
                self.texts = iter(("12", "13"))

            def recognize(self, frame, *, mode):
                return [
                    {
                        "text": next(self.texts),
                        "confidence": 0.99,
                        "x": 30,
                        "y": 24,
                        "width": 30,
                        "height": 20,
                    }
                ]

        result, diagnostic = reread_score_line(
            panel_frame(), self.observation(), DisagreeingOcr()
        )
        self.assertIsNone(result)
        self.assertEqual(diagnostic["status"], "score_line_width_disagreement")

    def test_off_center_label_cannot_leave_a_complete_leading_glyph_outside_both_crops(
        self,
    ):
        class DigitsOcr:
            def recognize(self, frame, *, mode):
                return [
                    {
                        "text": "123456",
                        "confidence": 0.99,
                        "x": 45,
                        "y": 24,
                        "width": 30,
                        "height": 20,
                    }
                ]

        observation = replace(
            self.observation(),
            features={
                "ocr_candidates": [
                    {"text": "SCORE", "confidence": 0.95, "bbox": (65, 30, 25, 10)}
                ]
            },
        )
        result, diagnostic = reread_score_line(panel_frame(), observation, DigitsOcr())
        self.assertIsNotNone(result, diagnostic)
        first, second = diagnostic["crop_bboxes"]
        self.assertLessEqual(first[0], 45)
        self.assertGreaterEqual(first[0] + first[2], 70)
        self.assertNotEqual(first, second)

    def test_missing_or_low_confidence_score_label_stays_unknown(self):
        class NeverOcr:
            def recognize(self, *args, **kwargs):
                raise AssertionError("unanchored OCR must not run")

        for candidates in (
            [],
            [{"text": "BEST", "confidence": 0.99, "bbox": (45, 30, 25, 10)}],
            [{"text": "SCORE", "confidence": 0.64, "bbox": (45, 30, 25, 10)}],
        ):
            result, _ = reread_score_line(
                panel_frame(),
                replace(self.observation(), features={"ocr_candidates": candidates}),
                NeverOcr(),
            )
            self.assertIsNone(result)
