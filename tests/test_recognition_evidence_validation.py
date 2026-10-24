import unittest

from tools.recognition_evidence_validation import parse_evidence_json, validate_recorded_visual_state


class EvidenceJsonValidationTests(unittest.TestCase):
    def test_rejects_duplicate_keys_at_any_depth_and_after_escape_decoding(self):
        for encoded in (b'{"a":1,"a":2}', b'{"diagnostics":{"a":1,"a":2}}', b'{"a":1,"\\u0061":2}'):
            with self.subTest(encoded=encoded), self.assertRaisesRegex(ValueError, "重複"):
                parse_evidence_json(encoded)

    def test_rejects_nonfinite_constants_and_exponent_overflow_anywhere(self):
        for number in (b"NaN", b"Infinity", b"-Infinity", b"1e999", b"-1e999"):
            for container in (b'{"confidence":%s}', b'{"diagnostics":[%s]}'):
                with self.subTest(number=number, container=container), self.assertRaises(ValueError):
                    parse_evidence_json(container % number)

    def test_preserves_finite_numbers_and_unknown_diagnostics(self):
        self.assertEqual(
            {"confidence": 0.01, "diagnostics": {"attempts": 3, "future": [None, "value", 1.0]}},
            parse_evidence_json(b'{"confidence":1e-2,"diagnostics":{"attempts":3,"future":[null,"value",1e0]}}'),
        )


class RecordedVisualStateValidationTests(unittest.TestCase):
    def test_accepts_missing_unknown_board_and_valid_cells(self):
        board = [[0, None, 2, 4], [8, 16, 32, 64], [128, 256, 512, 1024], [2048, 4096, 8192, 2**100]]
        for state in ({}, {"board": None}, {"board": board}, {"confidence": 0}, {"confidence": 1.0}):
            with self.subTest(state=state):
                validate_recorded_visual_state("last_capture", state)

    def test_rejects_non_object_game_state(self):
        for state in (None, [], "state", 0, False):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "game_state"):
                validate_recorded_visual_state("last_capture", state)

    def test_rejects_invalid_confidence_types_and_ranges(self):
        for confidence in (False, True, None, -0.1, 1.1, "0.5", [], {}, float("nan"), float("inf")):
            with self.subTest(confidence=confidence), self.assertRaisesRegex(ValueError, "confidence"):
                validate_recorded_visual_state("last_capture", {"confidence": confidence})

    def test_rejects_wrong_board_shape(self):
        for board in (False, "board", {}, [], [[0]], [[0] * 4] * 3, [[0] * 3] * 4):
            with self.subTest(board=board), self.assertRaisesRegex(ValueError, "board"):
                validate_recorded_visual_state("last_capture", {"board": board})

    def test_rejects_wrong_cell_types_and_non_tile_integers(self):
        for cell in (False, True, 0.0, 2.0, "2", -2, 1, 3, [], {}):
            board = [[0] * 4 for _ in range(4)]
            board[2][3] = cell
            with self.subTest(cell=cell), self.assertRaisesRegex(ValueError, r"board\[2\]\[3\]"):
                validate_recorded_visual_state("last_capture", {"board": board})


if __name__ == "__main__":
    unittest.main()
