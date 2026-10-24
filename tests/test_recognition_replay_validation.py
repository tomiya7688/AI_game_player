from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_game_player.screen_capture import ScreenFrame
from tools.recognition_failure_evidence import RecognitionFailureEvidence
from tools.replay_2048_recognition import main
from tools.replay_2048_recognition import RecognitionEvidenceReplay


class ReplayManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        frame = ScreenFrame(1, 1, bytes((0, 0, 0, 255)), captured_at=1.0)
        self.manifest = RecognitionFailureEvidence(Path(self.temporary.name)).save_failure(
            frame, {"board": None, "confidence": 0.0}, {}
        )

    def run_cli(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["replay", str(self.manifest)]), redirect_stdout(output), redirect_stderr(errors):
            try:
                code = main()
            except SystemExit as error:
                code = error.code
        return code, output.getvalue(), errors.getvalue()

    def test_boolean_confidence_is_an_input_error_not_a_zero_match(self):
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["frames"]["last_capture"]["game_state"]["confidence"] = False
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        code, output, errors = self.run_cli()
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIn("confidence", errors)

    def test_exponent_overflow_is_a_cli_input_error(self):
        raw = self.manifest.read_text(encoding="utf-8").replace('"confidence": 0.0', '"confidence": 1e999')
        self.manifest.write_text(raw, encoding="utf-8")
        code, output, errors = self.run_cli()
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIn("有限", errors)

    def test_duplicate_json_keys_are_not_silently_overwritten(self):
        raw = self.manifest.read_text(encoding="utf-8").replace('"schema":', '"schema": "untrusted", "schema":')
        self.manifest.write_text(raw, encoding="utf-8")
        code, output, errors = self.run_cli()
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertIn("重複", errors)

    def test_invalid_later_comparison_prevents_all_pixel_reads_and_recognition(self):
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        first = dict(payload["frames"]["last_capture"])
        payload["frames"] = {"last_playable": first, "last_capture": {"game_state": {"confidence": False}}}
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        replay = RecognitionEvidenceReplay(self.manifest)
        with patch.object(replay, "_read_frame") as read_frame, patch.object(replay.adapter, "adapt") as adapt:
            with self.assertRaisesRegex(ValueError, "confidence"):
                replay.run()
            read_frame.assert_not_called()
            adapt.assert_not_called()

    def test_valid_unrecognized_board_still_exits_zero(self):
        code, output, errors = self.run_cli()
        self.assertEqual(0, code)
        self.assertEqual("", errors)
        result = json.loads(output)["frames"]["last_capture"]
        self.assertEqual({"board": True, "confidence": True}, result["visual_matches"])
        self.assertIsNone(result["replayed_game_state"]["board"])


if __name__ == "__main__":
    unittest.main()
