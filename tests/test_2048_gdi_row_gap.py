from hashlib import sha256
import json
from pathlib import Path
import unittest
import zlib

from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.game_2048_board import Game2048BoardRecognizer, group_board_color_rows
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame


class Gdi2048RowGapTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).parent / "fixtures/2048"
        self.metadata = json.loads((directory / "gdi-row-gap.json").read_text(encoding="utf-8"))
        self.pixels = zlib.decompress((directory / "gdi-row-gap.bgra.zlib").read_bytes())

    def test_real_failure_frame_preserves_board_across_two_missing_palette_rows(self):
        metadata, pixels = self.metadata, self.pixels
        self.assertEqual(metadata["bgra_sha256"], sha256(pixels).hexdigest())
        frame = ScreenFrame(metadata["width"], metadata["height"], pixels)
        observation, candidates = ReferenceGame2048Adapter().adapt(
            frame, ScreenObservation("ci-gdi-failure-replay", frame.width, frame.height)
        )
        state = observation.features["game_2048"]
        self.assertEqual(metadata["expected_board"], state["board"])
        self.assertEqual(tuple(metadata["expected_board_bbox"]), state["board_bbox"])
        self.assertIs(state["game_over"], False)
        self.assertGreaterEqual(state["confidence"], 0.65)
        self.assertEqual(4, len(candidates))

    def test_gap_larger_than_board_padding_does_not_join_separate_regions(self):
        metadata = self.metadata
        pixels = bytearray(self.pixels)
        left, _, width, _ = metadata["expected_board_bbox"]
        # 375 pixel盤面の余白約11 pixelを超える16行は橋渡ししない。
        for y in range(470, 486):
            start = (y * metadata["width"] + left) * 4
            pixels[start:start + width * 4] = bytes((0, 0, 0, 255)) * width
        frame = ScreenFrame(metadata["width"], metadata["height"], bytes(pixels))
        self.assertIsNone(Game2048BoardRecognizer().recognize(frame))

    def test_grouping_preserves_empty_input_and_large_separation(self):
        self.assertEqual([], group_board_color_rows([]))
        rows = [(0, 0, 200, 200), (3, 0, 200, 200), (10, 0, 200, 200)]
        self.assertEqual([rows[:2], rows[2:]], group_board_color_rows(rows))

    def test_nearby_score_band_is_not_bridged_into_board(self):
        rows = [(0, 100, 210, 110), (4, 0, 200, 200)]
        self.assertEqual([[rows[0]], [rows[1]]], group_board_color_rows(rows))


if __name__ == "__main__":
    unittest.main()
