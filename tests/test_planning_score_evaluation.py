import unittest
from dataclasses import replace

from ai_game_player.capture_geometry import CaptureGeometry
from ai_game_player.capture_region import CaptureRegionMode, PixelRect
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.planning_score_evaluation import reread_score_line


class LineOcr:
    def __init__(self, texts=("1824", "1824"), edge=False):
        self.texts = iter(texts)
        self.frames = []
        self.edge = edge

    def recognize(self, frame, *, mode):
        self.frames.append(frame)
        assert mode == "single_numeric_line"
        return [
            {
                "text": next(self.texts),
                "x": 0 if self.edge else frame.width // 2 - 15,
                "y": 15,
                "width": 30,
                "height": 24,
                "confidence": 0.96,
            }
        ]


class PlanningScoreEvaluationTests(unittest.TestCase):
    def test_scaled_offset_geometry_maps_both_corners_of_label_and_numeric_box(self):
        for selected_window in (False, True):
            with self.subTest(window=selected_window):
                geometry = CaptureGeometry(
                    CaptureRegionMode.FULL_SCREEN,
                    PixelRect(0, 0, 200, 100),
                    200,
                    100,
                    PixelRect(40, 60, 400, 200),
                    PixelRect(40, 60, 400, 200) if selected_window else None,
                    1.0,
                    "fixture",
                )
                offset_x, offset_y = (0, 0) if selected_window else (40, 60)
                observation = ScreenObservation(
                    "score",
                    400,
                    200,
                    features={
                        "ocr_candidates": [
                            {
                                "text": "SCORE",
                                "bbox": (120 + offset_x, 20 + offset_y, 40, 10),
                                "confidence": 0.95,
                            }
                        ]
                    },
                )
                ocr = LineOcr()
                reread, _ = reread_score_line(
                    replace(self.frame, capture_geometry=geometry), observation, ocr
                )
                self.assertEqual([frame.width for frame in ocr.frames], [120, 180])
                self.assertEqual(
                    reread.features["ocr_candidates"][1]["bbox"],
                    (130 + offset_x, 40 + offset_y, 20, 16),
                )

    def setUp(self):
        self.frame = ScreenFrame(
            200, 100, bytes([1, 2, 3, 255]) * 20_000, captured_at=5
        )
        self.observation = ScreenObservation(
            "score",
            200,
            100,
            features={
                "ocr_candidates": [
                    {
                        "text": "SCORE",
                        "x": 60,
                        "y": 10,
                        "width": 20,
                        "height": 5,
                        "confidence": 0.95,
                    }
                ]
            },
        )

    def test_same_frame_two_width_readings_preserve_bbox_and_conservative_confidence(
        self,
    ):
        ocr = LineOcr()
        reread, diagnostic = reread_score_line(self.frame, self.observation, ocr)
        self.assertEqual(diagnostic["status"], "single_numeric_line_width_agreement")
        self.assertEqual(reread.features["ocr_candidates"][1]["bbox"], (65, 20, 10, 8))
        self.assertEqual(reread.features["ocr_candidates"][1]["confidence"], 0.96)
        self.assertEqual([frame.captured_at for frame in ocr.frames], [5, 5])
        self.assertEqual([frame.width for frame in ocr.frames], [120, 180])
        self.assertEqual(len(self.observation.features["ocr_candidates"]), 1)

    def test_truncated_or_width_disagreeing_digits_stay_unknown(self):
        for ocr, reason in [
            (LineOcr(edge=True), "score_line_touches_crop_edge"),
            (LineOcr(("824", "1824")), "score_line_width_disagreement"),
            (LineOcr(("noise",)), "score_line_not_unique_numeric"),
        ]:
            with self.subTest(reason=reason):
                reread, diagnostic = reread_score_line(
                    self.frame, self.observation, ocr
                )
                self.assertIsNone(reread)
                self.assertEqual(diagnostic["status"], reason)

    def test_missing_or_ambiguous_label_never_crops_a_board_number(self):
        for labels in ([], self.observation.features["ocr_candidates"] * 2):
            observation = ScreenObservation(
                "score", 200, 100, features={"ocr_candidates": labels}
            )
            ocr = LineOcr()
            reread, diagnostic = reread_score_line(self.frame, observation, ocr)
            self.assertIsNone(reread)
            self.assertEqual(ocr.frames, [])
