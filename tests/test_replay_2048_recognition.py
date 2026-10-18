from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.recognition_failure_evidence import RecognitionFailureEvidence
from tools.replay_2048_recognition import RecognitionEvidenceReplay, main


class RecognitionEvidenceReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(__file__).parent / "fixtures/2048/terminal-render"
        metadata = json.loads((directory / "terminal-0.75.json").read_text(encoding="utf-8"))
        pixels = zlib.decompress((directory / "terminal-0.75.bgra.zlib").read_bytes())
        self.frame = ScreenFrame(metadata["width"], metadata["height"], pixels)
        adapted, _ = ReferenceGame2048Adapter().adapt(
            self.frame, ScreenObservation("test-replay", self.frame.width, self.frame.height)
        )
        self.state = adapted.features["game_2048"]
        evidence = RecognitionFailureEvidence(Path(self.temporary.name))
        evidence.record_playable(self.frame, self.state)
        self.manifest = evidence.save_failure(self.frame, self.state, {"failure_detail": "test-only evidence"})

    def change_metadata(self, key, value):
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["frames"]["last_capture"][key] = value
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

    def test_replays_both_frames_and_matches_visual_state_without_ocr(self):
        report = RecognitionEvidenceReplay(self.manifest).run()
        self.assertIs(report["ocr_enabled"], False)
        self.assertEqual({"last_playable", "last_capture"}, report["frames"].keys())
        for frame in report["frames"].values():
            self.assertEqual(sha256(self.frame.bgra).hexdigest(), frame["bgra_sha256"])
            self.assertEqual({"board": True, "confidence": True}, frame["visual_matches"])
            self.assertIs(frame["replayed_game_state"]["game_over"], True)
            self.assertEqual(0, frame["candidate_count"])

    def test_hash_mismatch_is_rejected_before_recognition(self):
        self.change_metadata("bgra_sha256", "0" * 64)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            RecognitionEvidenceReplay(self.manifest).run()

    def test_outside_path_is_rejected(self):
        self.change_metadata("bgra", "../outside.bgra")
        with self.assertRaisesRegex(ValueError, "ファイル名"):
            RecognitionEvidenceReplay(self.manifest).run()

    def test_symlink_resolution_outside_bundle_is_rejected(self):
        replay = RecognitionEvidenceReplay(self.manifest)
        outside = self.manifest.parent.parent / "outside.bgra"
        with patch.object(Path, "resolve", return_value=outside):
            with self.assertRaisesRegex(ValueError, "bundle外"):
                replay.run()

    def test_nonfinite_manifest_number_is_rejected(self):
        self.change_metadata("game_state", {"confidence": float("nan")})
        with self.assertRaisesRegex(ValueError, "JSON標準外"):
            RecognitionEvidenceReplay(self.manifest).run()

    def test_invalid_dimensions_sizes_and_memory_limit_are_rejected(self):
        for width in (True, 0, self.frame.width + 1, 100_000_000):
            with self.subTest(width=width):
                self.change_metadata("width", width)
                with self.assertRaises(ValueError):
                    RecognitionEvidenceReplay(self.manifest).run()

    def test_cli_reports_visual_difference_with_exit_one(self):
        self.change_metadata("game_state", {"board": None, "confidence": 0.0})
        output = io.StringIO()
        with patch.object(sys, "argv", ["replay", str(self.manifest)]), redirect_stdout(output):
            self.assertEqual(1, main())
        result = json.loads(output.getvalue())["frames"]["last_capture"]
        self.assertEqual({"board": False, "confidence": False}, result["visual_matches"])

    def test_missing_visual_comparison_is_null_not_successful_match(self):
        self.change_metadata("game_state", {})
        report = RecognitionEvidenceReplay(self.manifest).run()
        self.assertEqual({"board": None, "confidence": None}, report["frames"]["last_capture"]["visual_matches"])

    def test_empty_frames_invalid_schema_and_manifest_size_are_rejected(self):
        for payload in ({"schema": "unknown"}, {"schema": "2048-recognition-evidence/v1", "frames": {}}):
            with self.subTest(payload=payload):
                self.manifest.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    RecognitionEvidenceReplay(self.manifest).run()
        self.manifest.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            RecognitionEvidenceReplay(self.manifest).run()

    def test_invalid_bundle_cli_has_actionable_error_and_no_report(self):
        self.change_metadata("bgra_sha256", "0" * 64)
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["replay", str(self.manifest)]),
            redirect_stdout(output), redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main()
        self.assertEqual(2, raised.exception.code)
        self.assertEqual("", output.getvalue())
        self.assertIn("元artifactを再取得", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
