import json
import unittest
from pathlib import Path

from ai_game_player.game_2048_adapter import (
    ReferenceGame2048Adapter,
)
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "2048"


def read_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def parse_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))


def render_fixture(fixture: dict) -> ScreenFrame:
    width, height = fixture["frame_size"]
    pixels = bytearray(width * height * 4)
    background = (250, 248, 239)
    for pixel_index in range(width * height):
        offset = pixel_index * 4
        pixels[offset : offset + 4] = bytes((*background[::-1], 255))
    style = fixture["css_colors"]
    board_rgb = parse_rgb(style["board_background"])
    empty_rgb = parse_rgb(style["empty_cell"])
    tile_colors = {int(value): parse_rgb(color) for value, color in style["tiles"].items()}
    left, top, board_width, board_height = fixture["board_rect"]
    for y in range(top, top + board_height):
        for x in range(left, left + board_width):
            write_rgb(pixels, width, x, y, board_rgb)
    board = fixture["board"]
    grid_layout = fixture["grid_layout"]
    for row in range(4):
        for column in range(4):
            cell_left = left + round(
                board_width * (grid_layout["inset_fraction"] + column * grid_layout["pitch_fraction"])
            )
            cell_top = top + round(
                board_height * (grid_layout["inset_fraction"] + row * grid_layout["pitch_fraction"])
            )
            cell_right = cell_left + round(board_width * grid_layout["cell_size_fraction"])
            cell_bottom = cell_top + round(board_height * grid_layout["cell_size_fraction"])
            cell_rgb = empty_rgb if board[row][column] == 0 else tile_colors[board[row][column]]
            for y in range(cell_top, cell_bottom):
                for x in range(cell_left, cell_right):
                    write_rgb(pixels, width, x, y, cell_rgb)
    score_background = board_rgb
    for y in range(95, 137):
        for x in range(165, 275):
            write_rgb(pixels, width, x, y, score_background)
    return ScreenFrame(width, height, bytes(pixels))


def render_game_over_fixture(fixture: dict) -> ScreenFrame:
    game_over_fixture = dict(fixture)
    game_over_fixture["board"] = [
        [2, 4, 2, 4],
        [4, 2, 4, 2],
        [2, 4, 2, 4],
        [4, 2, 4, 2],
    ]
    frame = render_fixture(game_over_fixture)
    pixels = bytearray(frame.bgra)
    width, height = fixture["frame_size"]
    left, top, board_width, board_height = fixture["board_rect"]
    overlay_color = parse_rgb(fixture["game_over_overlay"]["color"])
    overlay_opacity = fixture["game_over_overlay"]["opacity"]
    for y in range(top, top + board_height):
        for x in range(left, left + board_width):
            offset = (y * width + x) * 4
            current_rgb = (pixels[offset + 2], pixels[offset + 1], pixels[offset])
            overlay_rgb = tuple(
                round((1 - overlay_opacity) * channel + overlay_opacity * overlay)
                for channel, overlay in zip(current_rgb, overlay_color)
            )
            write_rgb(pixels, width, x, y, overlay_rgb)
    return ScreenFrame(width, height, bytes(pixels))


def write_rgb(pixels: bytearray, width: int, x: int, y: int, rgb: tuple[int, int, int]) -> None:
    offset = (y * width + x) * 4
    pixels[offset : offset + 4] = bytes((rgb[2], rgb[1], rgb[0], 255))


class ReferenceGame2048AdapterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = read_fixture("standard.json")
        self.adapter = ReferenceGame2048Adapter()

    def observation(self, ocr_candidates=None, ocr_text=None):
        return ScreenObservation(
            "2048",
            *self.fixture["frame_size"],
            ocr_text or ["SCORE", "1234", "BEST", "4096"],
            {"ocr_candidates": ocr_candidates or self.fixture["ocr_candidates"]},
        )

    def test_screen_fixture_produces_board_score_signature_and_four_arrows(self):
        frame = render_fixture(self.fixture)

        adapted, candidates = self.adapter.adapt(frame, self.observation())

        state = adapted.features["game_2048"]
        self.assertEqual(state["board"], self.fixture["board"])
        self.assertEqual(state["score"], self.fixture["score"])
        self.assertGreater(state["score_confidence"], 0.9)
        self.assertFalse(state["game_over"])
        self.assertEqual(len(state["state_signature"]), 64)
        self.assertEqual([candidate.label for candidate in candidates], ["UP", "DOWN", "LEFT", "RIGHT"])
        self.assertTrue(all(candidate.kind == "key" for candidate in candidates))
        self.assertTrue(all(candidate.confidence >= self.adapter.minimum_candidate_confidence for candidate in candidates))

    def test_screen_fixture_recognizes_board_colors_shifted_by_capture_resampling(self):
        frame = render_fixture(self.fixture)
        pixels = bytearray(frame.bgra)
        left, top, board_width, board_height = self.fixture["board_rect"]
        for y in range(top, top + board_height):
            for x in range(left, left + board_width):
                offset = (y * frame.width + x) * 4
                shifted_rgb = tuple(
                    min(255, channel + 2)
                    for channel in (pixels[offset + 2], pixels[offset + 1], pixels[offset])
                )
                write_rgb(pixels, frame.width, x, y, shifted_rgb)
        resampled_frame = ScreenFrame(frame.width, frame.height, bytes(pixels))

        adapted, candidates = self.adapter.adapt(resampled_frame, self.observation())

        self.assertEqual(adapted.features["game_2048"]["board"], self.fixture["board"])
        self.assertEqual([candidate.label for candidate in candidates], ["UP", "DOWN", "LEFT", "RIGHT"])

    def test_screen_fixture_selects_current_score_instead_of_best_score(self):
        frame = render_fixture(self.fixture)

        adapted, _ = self.adapter.adapt(frame, self.observation())

        self.assertEqual(adapted.features["game_2048"]["score"], 1234)
        self.assertNotEqual(adapted.features["game_2048"]["score"], self.fixture["best_score"])

    def test_state_signature_is_repeatable_and_changes_when_current_score_changes(self):
        frame = render_fixture(self.fixture)
        original_observation = self.observation()
        changed_candidates = [dict(candidate) for candidate in self.fixture["ocr_candidates"]]
        changed_candidates[1]["text"] = "1235"

        original, _ = self.adapter.adapt(frame, original_observation)
        repeated, _ = self.adapter.adapt(frame, original_observation)
        changed_score, _ = self.adapter.adapt(
            frame,
            self.observation(ocr_candidates=changed_candidates),
        )

        original_signature = original.features["game_2048"]["state_signature"]
        self.assertEqual(original_signature, repeated.features["game_2048"]["state_signature"])
        self.assertNotEqual(original_signature, changed_score.features["game_2048"]["state_signature"])

    def test_low_board_confidence_does_not_produce_arrow_candidates(self):
        frame = render_fixture(self.fixture)
        pixels = bytearray(frame.bgra)
        left, top, board_width, _ = self.fixture["board_rect"]
        layout = self.fixture["grid_layout"]
        cell_left = left + round(board_width * layout["inset_fraction"])
        cell_top = top + round(board_width * layout["inset_fraction"])
        cell_width = round(board_width * layout["cell_size_fraction"])
        for y in range(cell_top, cell_top + cell_width):
            for x in range(cell_left, cell_left + cell_width):
                write_rgb(pixels, frame.width, x, y, (1, 2, 3))
        uncertain_frame = ScreenFrame(frame.width, frame.height, bytes(pixels))
        strict_adapter = ReferenceGame2048Adapter(minimum_candidate_confidence=1.0)

        adapted, candidates = strict_adapter.adapt(uncertain_frame, self.observation())

        self.assertLess(adapted.features["game_2048"]["confidence"], 1.0)
        self.assertEqual(candidates, [])

    def test_game_over_fixture_suppresses_arrow_candidates(self):
        frame = render_game_over_fixture(self.fixture)
        observation = self.observation(ocr_candidates=[], ocr_text=[])

        adapted, candidates = self.adapter.adapt(frame, observation)

        self.assertTrue(adapted.features["game_2048"]["game_over"])
        self.assertEqual(candidates, [])

    def test_game_over_ocr_text_is_detected_when_board_is_incomplete(self):
        frame = render_fixture(self.fixture)
        observation = self.observation(ocr_candidates=[], ocr_text=["GAME OVER!"])

        adapted, candidates = self.adapter.adapt(frame, observation)

        self.assertTrue(adapted.features["game_2048"]["game_over"])
        self.assertEqual(candidates, [])

    def test_game_over_ocr_text_is_reported_when_board_pixels_are_unrecognized(self):
        width, height = self.fixture["frame_size"]
        blank = bytearray([239, 248, 250, 255] * width * height)
        observation = self.observation(ocr_candidates=[], ocr_text=["GAME OVER!"])

        adapted, candidates = self.adapter.adapt(
            ScreenFrame(width, height, bytes(blank)),
            observation,
        )

        self.assertIsNone(adapted.features["game_2048"]["board"])
        self.assertTrue(adapted.features["game_2048"]["game_over"])
        self.assertEqual(candidates, [])

    def test_unrelated_screen_does_not_produce_board_or_actions(self):
        width, height = self.fixture["frame_size"]
        blank = bytearray([239, 248, 250, 255] * width * height)

        adapted, candidates = self.adapter.adapt(
            ScreenFrame(width, height, bytes(blank)),
            self.observation(ocr_candidates=[], ocr_text=[]),
        )

        self.assertIsNone(adapted.features["game_2048"]["board"])
        self.assertEqual(adapted.features["game_2048"]["confidence"], 0.0)
        self.assertEqual(candidates, [])

    def test_invalid_frame_bytes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "BGRA buffer size"):
            self.adapter.adapt(
                ScreenFrame(2, 2, bytes(4)),
                ScreenObservation("2048", 2, 2),
            )

    def test_nonpositive_frame_dimensions_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "dimensions must be positive"):
            self.adapter.adapt(
                ScreenFrame(0, 0, b""),
                ScreenObservation("2048", 0, 0),
            )



if __name__ == "__main__":
    unittest.main()
