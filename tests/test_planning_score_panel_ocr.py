"""実Win画像を独立Tesseract CLIで読む。optional OCR未導入時だけskipする。"""

import csv
from dataclasses import replace
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import shutil
import struct
import subprocess
import unittest
import zlib

from ai_game_player.capture_geometry import CaptureGeometry
from ai_game_player.capture_region import CaptureRegionMode, PixelRect
from ai_game_player.game_2048_adapter import _read_score
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.planning_score_evaluation import reread_score_header_label, reread_score_line


class IndependentCliOcr:
    def recognize(self, frame, *, mode):
        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )

        rows = []
        for y in range(frame.height):
            row = frame.bgra[y * frame.width * 4 : (y + 1) * frame.width * 4]
            rows.append(
                b"\0"
                + b"".join(
                    row[x + 2 : x + 3] + row[x + 1 : x + 2] + row[x : x + 1]
                    for x in range(0, len(row), 4)
                )
            )
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", frame.width, frame.height, 8, 2, 0, 0, 0),
            )
            + chunk(b"IDAT", zlib.compress(b"".join(rows)))
            + chunk(b"IEND", b"")
        )
        command = [
            shutil.which("tesseract"),
            "stdin",
            "stdout",
            "--psm",
            "6" if mode == "text_block" else "7",
        ]
        if mode == "single_numeric_line":
            command += ["-c", "tessedit_char_whitelist=0123456789,"]
        result = subprocess.run(
            command + ["tsv"],
            input=png,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=20,
        )
        return [
            {
                "text": word["text"].strip(),
                "confidence": float(word["conf"]) / 100,
                **{
                    key: int(word[source])
                    for key, source in (
                        ("x", "left"),
                        ("y", "top"),
                        ("width", "width"),
                        ("height", "height"),
                    )
                },
            }
            for word in csv.DictReader(StringIO(result.stdout.decode()), delimiter="\t")
            if word["text"].strip() and float(word["conf"]) > 0
        ]


@unittest.skipUnless(
    shutil.which("tesseract"),
    "actual score OCR regression requires optional Tesseract CLI",
)
class WindowsHeaderOcrTests(unittest.TestCase):
    def test_nine_owned_header_images_and_scaled_geometry_recover_visible_scores(self):
        folder = Path(__file__).parent / "fixtures/2048/score-windows-headers"
        manifest = json.loads((folder / "manifest.json").read_text())
        for item in manifest["cases"]:
            raw = zlib.decompress((folder / item["bgra_zlib"]).read_bytes())
            self.assertEqual(sha256(raw).hexdigest(), item["header_bgra_sha256"])
            self.assertEqual(len(raw), 640 * 240 * 4)
            base = bytearray(b"\xff" * (904 * 648 * 4))
            for y in range(240):
                offset = (y * 904 + 202) * 4
                base[offset : offset + 640 * 4] = raw[y * 640 * 4 : (y + 1) * 640 * 4]
            frame = ScreenFrame(904, 648, bytes(base), captured_at=19)
            for scaled in (False, True):
                with self.subTest(image=item["image_id"], scaled=scaled):
                    current = frame
                    bbox = tuple(item["board_bbox"])
                    if scaled:
                        geometry = CaptureGeometry(
                            CaptureRegionMode.WINDOW,
                            PixelRect(0, 0, 904, 648),
                            904,
                            648,
                            PixelRect(40, 60, 1808, 1296),
                            PixelRect(10, 20, 1848, 1356),
                            1,
                            "fixture 2x/offset",
                        )
                        current = replace(frame, capture_geometry=geometry)
                        x, y = geometry.roi_to_window(260, 232)
                        right, bottom = geometry.roi_to_window(635, 607)
                        bbox = (x, y, right - x, bottom - y)
                    observation = ScreenObservation(
                        "header",
                        1808 if scaled else 904,
                        1296 if scaled else 648,
                        features={"ocr_candidates": []},
                    )
                    ocr = IndependentCliOcr()
                    header, diagnostic = reread_score_header_label(
                        current, observation, bbox, ocr
                    )
                    self.assertIsNotNone(header, diagnostic)
                    line, diagnostic = reread_score_line(current, header, ocr)
                    self.assertIsNotNone(line, diagnostic)
                    score, confidence = _read_score(line, bbox)
                    self.assertEqual(score, item["visible_score"])
                    self.assertGreaterEqual(confidence, 0.65)
                    self.assertEqual(observation.features["ocr_candidates"], [])
                    self.assertEqual(current.bgra, frame.bgra)

    def test_blank_header_does_not_invent_score_or_title_value(self):
        frame = ScreenFrame(904, 648, b"\xff" * (904 * 648 * 4), captured_at=1)
        header, _ = reread_score_header_label(
            frame,
            ScreenObservation("blank", 904, 648),
            (260, 232, 375, 375),
            IndependentCliOcr(),
        )
        self.assertIsNone(header)
