from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import unittest
import zlib

from ai_game_player.capture_geometry import CaptureGeometry
from ai_game_player.capture_region import CaptureRegionMode, PixelRect
from ai_game_player.game_2048_adapter import _read_score
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.planning_score_evaluation import reread_score_line

FOLDER = Path(__file__).parent / "fixtures/2048/score-windows-headers"


def zero_fixture(scaled=False):
    manifest = json.loads((FOLDER / "manifest.json").read_text())
    item = next(
        item
        for item in manifest["cases"]
        if item["image_id"] == "124944163043-planner-481"
    )
    raw = zlib.decompress((FOLDER / item["bgra_zlib"]).read_bytes())
    frame = bytearray(b"\xff" * (904 * 648 * 4))
    for y in range(240):
        frame[(y * 904 + 202) * 4 : (y * 904 + 842) * 4] = raw[
            y * 640 * 4 : (y + 1) * 640 * 4
        ]
    source = ScreenFrame(904, 648, bytes(frame), captured_at=19)
    label = (542, 99, 30, 7)
    board = (260, 232, 375, 375)
    if scaled:
        geometry = CaptureGeometry(
            CaptureRegionMode.WINDOW,
            PixelRect(0, 0, 904, 648),
            904,
            648,
            PixelRect(40, 60, 1808, 1296),
            PixelRect(10, 20, 1848, 1356),
            1,
            "fixture",
        )
        source = replace(source, capture_geometry=geometry)
        x, y = geometry.roi_to_window(label[0], label[1])
        right, bottom = geometry.roi_to_window(label[0] + label[2], label[1] + label[3])
        label = (x, y, right - x, bottom - y)
        x, y = geometry.roi_to_window(board[0], board[1])
        right, bottom = geometry.roi_to_window(board[0] + board[2], board[1] + board[3])
        board = (x, y, right - x, bottom - y)
    obs = ScreenObservation(
        "zero",
        1808 if scaled else 904,
        1296 if scaled else 648,
        features={
            "ocr_candidates": [{"text": "SCORE", "confidence": 0.96, "bbox": label}]
        },
    )
    return source, obs, board


class RawWindowsOcr:
    def __init__(self):
        self.raw = json.loads((FOLDER / "windows_919_numeric_raw.json").read_text())[
            "records"
        ]
        self.calls = []

    def recognize(self, frame, *, mode):
        rgb = b"".join(
            frame.bgra[i + 2 : i + 3]
            + frame.bgra[i + 1 : i + 2]
            + frame.bgra[i : i + 1]
            for i in range(0, len(frame.bgra), 4)
        )
        fingerprint = sha256(rgb).hexdigest()
        matches = [
            r
            for r in self.raw
            if r["rgb_sha256"] == fingerprint
            and r["whitelist"] == (mode == "single_numeric_line")
        ]
        if len(matches) != 1:
            raise AssertionError((mode, fingerprint, len(matches)))
        self.calls.append((mode, fingerprint))
        return [
            {
                "text": w["text"],
                "confidence": w["confidence"] / 100,
                "x": w["bbox"][0],
                "y": w["bbox"][1],
                "width": w["bbox"][2],
                "height": w["bbox"][3],
            }
            for w in matches[0]["words"]
            if w["confidence"] > 0
        ]


def word(text="12", confidence=0.9, x=24):
    return {
        "text": text,
        "confidence": confidence,
        "x": x,
        "y": 24,
        "width": 20,
        "height": 30,
    }


class ProfileOcr:
    def __init__(self, binary, gray):
        self.binary = iter(binary)
        self.gray = iter(gray)
        self.modes = []

    def recognize(self, frame, *, mode):
        self.modes.append(mode)
        return next(self.binary if mode == "single_numeric_line" else self.gray)


class GrayFallbackTests(unittest.TestCase):
    def test_exact_pixel_raw_windows_replay_and_scaled_coordinate_mapping(self):
        for scaled in (False, True):
            with self.subTest(scaled=scaled):
                frame, obs, board = zero_fixture(scaled)
                original = deepcopy(obs)
                data = frame.bgra
                ocr = RawWindowsOcr()
                result, diag = reread_score_line(frame, obs, ocr)
                self.assertIsNotNone(result, diag)
                value, confidence = _read_score(result, board)
                self.assertEqual(value, 0)
                self.assertAlmostEqual(confidence, 0.68712212)
                self.assertEqual(
                    diag["preprocessing"], "bilinear_grayscale_unrestricted_fallback"
                )
                self.assertEqual(
                    [m for m, h in ocr.calls],
                    ["single_numeric_line"] * 2 + ["unrestricted_numeric_line"] * 2,
                )
                self.assertEqual(obs, original)
                self.assertEqual(frame.bgra, data)

    def test_binary_success_never_reads_unrestricted_fallback(self):
        frame, obs, _ = zero_fixture()
        ocr = ProfileOcr([[word()], [word()]], [])
        result, _ = reread_score_line(frame, obs, ocr)
        self.assertIsNotNone(result)
        self.assertEqual(ocr.modes, ["single_numeric_line"] * 2)

    def test_partial_binary_requires_same_fallback_value(self):
        for fallback, accepted in (("12", True), ("13", False)):
            with self.subTest(fallback=fallback):
                frame, obs, _ = zero_fixture()
                ocr = ProfileOcr(
                    [[word("12")], []], [[word(fallback)], [word(fallback)]]
                )
                result, diag = reread_score_line(frame, obs, ocr)
                self.assertEqual(result is not None, accepted)
                if not accepted:
                    self.assertEqual(diag["status"], "score_line_profile_disagreement")

    def test_binary_conflict_ambiguity_low_confidence_or_truncation_cannot_fallback(
        self,
    ):
        for binary in (
            ([word("12")], [word("13")]),
            ([word("12"), word("13")], []),
            ([word(confidence=0.64)], []),
            ([word(x=12)], []),
        ):
            with self.subTest(binary=binary):
                frame, obs, _ = zero_fixture()
                ocr = ProfileOcr(binary, [])
                result, _ = reread_score_line(frame, obs, ocr)
                self.assertIsNone(result)
                self.assertNotIn("unrestricted_numeric_line", ocr.modes)

    def test_gray_disagreement_unknown_text_low_confidence_or_edge_stays_unknown(self):
        for gray in (
            ([word("12")], [word("13")]),
            ([word("O")], [word("O")]),
            ([word("‘")], [word("‘")]),
            ([word(confidence=0.64)], [word(confidence=0.64)]),
            ([word(x=12)], [word(x=12)]),
            ([], []),
        ):
            with self.subTest(gray=gray):
                frame, obs, _ = zero_fixture()
                ocr = ProfileOcr([[], []], gray)
                result, _ = reread_score_line(frame, obs, ocr)
                self.assertIsNone(result)
