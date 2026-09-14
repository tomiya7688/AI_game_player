import unittest
from pathlib import Path

from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.screen_capture import ScreenFrame


FIXTURES = Path(__file__).parent / "fixtures" / "recognition"


def load_ppm(path: Path) -> ScreenFrame:
    tokens = path.read_text(encoding="ascii").split()
    if tokens[0] != "P3":
        raise ValueError("fixture must be an ASCII PPM image")
    width, height, max_value = int(tokens[1]), int(tokens[2]), int(tokens[3])
    if max_value != 255:
        raise ValueError("fixture must use 8-bit RGB values")
    values = [int(value) for value in tokens[4:]]
    if len(values) != width * height * 3:
        raise ValueError("fixture pixel count does not match dimensions")
    bgra = bytearray()
    for index in range(0, len(values), 3):
        red, green, blue = values[index : index + 3]
        bgra.extend((blue, green, red, 255))
    return ScreenFrame(width, height, bytes(bgra))


class FrameAnalyzerTest(unittest.TestCase):
    def test_extracts_color_and_visual_signatures(self):
        frame = ScreenFrame(2, 1, bytes([10, 20, 30, 255, 10, 20, 30, 255]))
        observation = FrameAnalyzer().analyze(frame, "test")
        self.assertEqual(observation.features["mean_rgb"], {"r": 30, "g": 20, "b": 10})
        self.assertEqual(len(observation.features["signature"]), 64)
        self.assertEqual(len(observation.features["perceptual_hash"]), 16)

    def test_fixture_image_emits_common_detected_element_and_legacy_candidate(self):
        observation = FrameAnalyzer().analyze(load_ppm(FIXTURES / "bright_button.ppm"), "fixture")
        elements = observation.features["detected_elements"]
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["source"], "bright_region")
        self.assertEqual(elements[0]["bbox"], (2, 1, 3, 3))
        candidates = observation.features["image_candidates"]
        self.assertEqual((candidates[0]["x"], candidates[0]["y"]), (3, 2))

    def test_rejects_invalid_buffer(self):
        with self.assertRaises(ValueError):
            FrameAnalyzer().analyze(ScreenFrame(1, 1, b"bad"))