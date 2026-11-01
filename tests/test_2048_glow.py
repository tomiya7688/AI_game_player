from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import zlib

from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter, _read_score
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame

FIXTURES = Path(__file__).parent / "fixtures/2048"


def glow_frame(name="glow-256"):

    metadata = json.loads((FIXTURES / (name + ".json")).read_text())
    pixels = zlib.decompress((FIXTURES / (name + ".bgra.zlib")).read_bytes())
    assert sha256(pixels).hexdigest() == metadata["bgra_sha256"]
    return metadata, ScreenFrame(metadata["width"], metadata["height"], pixels)


class GlowRecognitionTests(unittest.TestCase):
    def test_real_glow_frame_recovers_geometry_and_all_cells(self):
        for name in ("glow-256", "glow-256-seed480"):
            with self.subTest(name=name):
                metadata, frame = glow_frame(name)
                observation, candidates = ReferenceGame2048Adapter().adapt(
                    frame, ScreenObservation("real-glow", frame.width, frame.height)
                )
                state = observation.features["game_2048"]
                self.assertEqual(
                    tuple(metadata["expected_board_bbox"]), state["board_bbox"]
                )
                self.assertEqual(metadata["expected_board"], state["board"])
                self.assertEqual(len(candidates), 4)

    def test_unlocated_heading_after_score_label_is_unknown(self):
        observation = ScreenObservation(
            "ambiguous", 650, 850, ["SCORE", "2048", "1824", "BEST"]
        )
        self.assertEqual((None, 0.0), _read_score(observation, (75, 274, 500, 500)))

    def test_heading_without_score_label_cannot_be_current_score(self):
        observation = ScreenObservation(
            "heading",
            650,
            850,
            features={
                "ocr_candidates": [
                    {"text": "2048", "confidence": 0.99, "bbox": [75, 100, 190, 70]},
                ]
            },
        )
        self.assertEqual((None, 0.0), _read_score(observation, (75, 274, 500, 500)))

    def test_score_label_does_not_pair_with_distant_heading(self):
        observation = ScreenObservation(
            "heading",
            650,
            850,
            features={
                "ocr_candidates": [
                    {"text": "SCORE", "confidence": 0.99, "bbox": [384, 100, 40, 10]},
                    {"text": "2048", "confidence": 0.99, "bbox": [75, 100, 190, 70]},
                ]
            },
        )
        self.assertEqual((None, 0.0), _read_score(observation, (75, 274, 500, 500)))

    def test_glyph_truth_is_supported_by_independent_pixel_mask(self):
        _, frame = glow_frame()

        def mask(left):
            points = set()
            for y in range(106):
                for x in range(106):
                    offset = ((531 + y) * frame.width + left + x) * 4
                    if max(frame.bgra[offset : offset + 3]) < 155:
                        points.add((x, y))
            return points

        left, right = mask(90), mask(454)
        self.assertEqual(537, len(left))
        self.assertEqual(537, len(right))
        self.assertEqual(right, {(x - 1, y) for x, y in left})

    def test_css_glow_scale_position_and_overlay_fixtures(self):
        paths = sorted((FIXTURES / "glow-render").glob("*.json"))
        self.assertEqual(6, len(paths))
        for path in paths:
            with self.subTest(path=path.name):
                metadata = json.loads(path.read_text())
                pixels = zlib.decompress(path.with_suffix(".bgra.zlib").read_bytes())
                self.assertEqual(metadata["bgra_sha256"], sha256(pixels).hexdigest())
                frame = ScreenFrame(metadata["width"], metadata["height"], pixels)
                observation, candidates = ReferenceGame2048Adapter().adapt(
                    frame, ScreenObservation("css-glow", frame.width, frame.height)
                )
                state = observation.features["game_2048"]
                self.assertEqual(metadata["expected_board"], state["board"])
                if metadata["overlay"]:
                    self.assertTrue(state["game_over"])
                    self.assertEqual([], candidates)
                # CSS layout has fractional device pixels; each raster boundary is within 1px.
                for expected, actual in zip(
                    metadata["expected_board_bbox"], state["board_bbox"]
                ):
                    self.assertLessEqual(abs(expected - actual), 1)

    def test_unknown_color_is_not_filled_and_has_no_candidates(self):
        _, frame = glow_frame()
        pixels = bytearray(frame.bgra)
        for y in range(289, 395):
            start = (y * frame.width + 90) * 4
            pixels[start : start + 106 * 4] = bytes((255, 255, 0, 255)) * 106
        observation, candidates = ReferenceGame2048Adapter().adapt(
            ScreenFrame(frame.width, frame.height, bytes(pixels)),
            ScreenObservation("unknown", frame.width, frame.height),
        )
        self.assertIsNone(observation.features["game_2048"]["board"][0][0])
        self.assertEqual([], candidates)

    def test_located_current_score_is_separate_from_header_and_best(self):
        observation = ScreenObservation(
            "score",
            650,
            850,
            features={
                "ocr_candidates": [
                    {"text": "2048", "confidence": 0.99, "bbox": [75, 100, 190, 70]},
                    {"text": "SCORE", "confidence": 0.99, "bbox": [384, 100, 40, 10]},
                    {"text": "1824", "confidence": 0.96, "bbox": [376, 118, 58, 22]},
                    {"text": "BEST", "confidence": 0.99, "bbox": [500, 100, 40, 10]},
                    {"text": "4096", "confidence": 0.99, "bbox": [490, 118, 58, 22]},
                ]
            },
        )
        self.assertEqual((1824, 0.96), _read_score(observation, (75, 274, 500, 500)))
        observation.features["ocr_candidates"].append(
            {"text": "1900", "confidence": 0.99, "bbox": [376, 119, 58, 22]}
        )
        self.assertEqual((None, 0.0), _read_score(observation, (75, 274, 500, 500)))

    def test_only_an_explicit_inline_score_is_accepted_without_positions(self):
        for texts, expected in (
            (["SCORE 1,824"], (1824, 0.5)),
            (["SCORE 1824", "SCORE 1900"], (None, 0.0)),
            (["BEST 1824"], (None, 0.0)),
        ):
            with self.subTest(texts=texts):
                self.assertEqual(
                    expected,
                    _read_score(ScreenObservation("inline", 650, 850, texts), None),
                )

    def test_browser_probe_keeps_score_bounding_box_evidence(self):
        from tools.check_2048_screen_transition import BrowserScreenSource

        _, frame = glow_frame()
        words = [
            {"text": "2048", "confidence": 0.99, "bbox": [75, 100, 190, 70]},
            {"text": "SCORE", "confidence": 0.99, "bbox": [384, 100, 40, 10]},
            {"text": "1824", "confidence": 0.96, "bbox": [376, 118, 58, 22]},
        ]

        class Image:
            width, height = frame.width, frame.height

            def convert(self, _mode):
                return self

            def tobytes(self, *_args):
                return frame.bgra

        source = BrowserScreenSource(
            SimpleNamespace(screenshot=lambda: b"isolated fixture"),
            SimpleNamespace(open=lambda _stream: Image()),
            SimpleNamespace(recognize=lambda _frame: words),
        )
        adapter = ReferenceGame2048Adapter()
        calls = []

        def adapt(frame, observation):
            calls.append(observation)
            result = adapter.adapt(frame, observation)
            if len(calls) == 1:
                result[0].features["game_2048"]["confidence"] = 0.5
            return result

        source.adapter = SimpleNamespace(adapt=adapt)
        source.capture()
        observed, _ = source.read()
        self.assertEqual(words, observed.features["ocr_candidates"])
        self.assertEqual(1824, observed.features["game_2048"]["score"])
        self.assertEqual(0.96, observed.features["game_2048"]["score_confidence"])
