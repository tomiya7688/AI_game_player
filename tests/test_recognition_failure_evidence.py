import json
from hashlib import sha256
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from ai_game_player.campaign_runner import CampaignFailure
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.models import ScreenObservation
from tools.recognition_failure_evidence import RecognitionFailureEvidence
from tools import run_2048_campaign


def _make_source(directory: Path, game_state: dict, candidates: list | None = None):
    frame = ScreenFrame(1, 1, bytes((10, 20, 30, 0)), captured_at=12.5, frame_id=7)
    observation = SimpleNamespace(features={"signature": "frame-seven"})
    adapted = ScreenObservation("fixture", 1, 1, features={"game_2048": game_state})
    source = run_2048_campaign.Cached2048ObservationSource.__new__(run_2048_campaign.Cached2048ObservationSource)
    source.window_handle = 123
    source.source_id = "fixture_capture"
    source.capture_sequence = 0
    source.not_before = 0.0
    source.capture = SimpleNamespace(capture=Mock(return_value=frame), last_backend_error=None)
    source.analyzer = SimpleNamespace(analyze=Mock(return_value=observation))
    source.adapter = SimpleNamespace(
        adapt=Mock(return_value=(adapted, candidates or [])), minimum_candidate_confidence=0.65
    )
    source.failure_evidence = RecognitionFailureEvidence(directory)
    source._cached_read = None
    return source, frame


class RecognitionFailureEvidenceTests(unittest.TestCase):
    def test_bundle_preserves_pixels_orientation_and_recognition_context(self):
        bgra = bytes((0, 0, 255, 0, 0, 255, 0, 255, 255, 0, 0, 1, 255, 255, 255, 255))
        frame = ScreenFrame(2, 2, bgra, captured_at=12.5, frame_id=7)
        accepted_state = {"board": [[2]], "confidence": 0.9}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "episode-0001"
            evidence = RecognitionFailureEvidence(directory)
            evidence.record_playable(frame, accepted_state)
            self.assertFalse(directory.exists())
            accepted_state["board"][0][0] = 64
            manifest = evidence.save_failure(frame, {"board": None}, {"failure_detail": "no board"})
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            accepted = payload["frames"]["last_playable"]
            captured = payload["frames"]["last_capture"]
            self.assertEqual([[2]], accepted["game_state"]["board"])
            self.assertIsNone(captured["game_state"]["board"])
            self.assertEqual(sha256(bgra).hexdigest(), captured["bgra_sha256"])
            self.assertEqual(bgra, (manifest.parent / captured["bgra"]).read_bytes())
            self.assertEqual(7, captured["frame_id"])
            self.assertEqual(12.5, captured["captured_at"])
            png = (manifest.parent / captured["png"]).read_bytes()

        self.assertEqual(b"\x89PNG\r\n\x1a\n", png[:8])
        chunks = {}
        offset = 8
        while offset < len(png):
            length = struct.unpack_from(">I", png, offset)[0]
            kind = png[offset + 4:offset + 8]
            data = png[offset + 8:offset + 8 + length]
            checksum = struct.unpack_from(">I", png, offset + 8 + length)[0]
            self.assertEqual(zlib.crc32(kind + data), checksum)
            chunks[kind] = data
            offset += length + 12
        self.assertEqual((2, 2, 8, 2, 0, 0, 0), struct.unpack(">IIBBBBB", chunks[b"IHDR"]))
        # Two RGB rows with PNG filter 0: red/green, then blue/white, regardless of BGRA alpha.
        expected_rows = bytes((0, 255, 0, 0, 0, 255, 0, 0, 0, 0, 255, 255, 255, 255))
        self.assertEqual(expected_rows, zlib.decompress(chunks[b"IDAT"]))

    def test_new_failure_bundle_never_overwrites_existing_evidence(self):
        frame = ScreenFrame(1, 1, bytes((0, 0, 255, 255)))
        with tempfile.TemporaryDirectory() as temporary:
            evidence = RecognitionFailureEvidence(Path(temporary))
            first = evidence.save_failure(frame, {}, {"failure_detail": "first"})
            original = first.read_bytes()
            second = evidence.save_failure(frame, {}, {"failure_detail": "second"})
            self.assertNotEqual(first.parent, second.parent)
            self.assertEqual(original, first.read_bytes())

    def test_timeout_keeps_failed_capture_and_threshold_in_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, frame = _make_source(Path(temporary), {"board": None, "confidence": 0.0})
            ticks = iter((0.0, 0.5, 2.0, 2.0))
            with (
                patch.object(run_2048_campaign, "SCREEN_READY_TIMEOUT_SECONDS", 1.0),
                patch.object(run_2048_campaign.time, "monotonic", side_effect=lambda: next(ticks)),
                patch.object(run_2048_campaign.time, "sleep"),
            ):
                with self.assertRaises(CampaignFailure) as raised:
                    source.capture_playable_state()
            manifests = list(Path(temporary).glob("recognition-failure-*/manifest.json"))
            self.assertEqual(1, len(manifests))
            payload = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertIn(f"failure_evidence={manifests[0]}", raised.exception.detail)
            self.assertEqual("recognition_failure", raised.exception.category)
            self.assertEqual(0.65, payload["diagnostics"]["candidate_confidence_threshold"])
            self.assertEqual("frame-seven", payload["diagnostics"]["last_frame_signature"])
            self.assertEqual(123, payload["diagnostics"]["window_handle"])
            self.assertEqual(frame.bgra, (manifests[0].parent / "last-captured.bgra").read_bytes())

    def test_analysis_exception_keeps_frame_and_original_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, frame = _make_source(Path(temporary), {})
            source.analyzer.analyze.side_effect = ValueError("analysis rejected frame")
            with self.assertRaises(CampaignFailure) as raised:
                source.capture_playable_state()
            self.assertEqual("recognition_failure", raised.exception.category)
            self.assertIn("analysis rejected frame", raised.exception.detail)
            manifest = next(Path(temporary).glob("recognition-failure-*/manifest.json"))
            self.assertEqual(frame.bgra, (manifest.parent / "last-captured.bgra").read_bytes())
            self.assertIsInstance(raised.exception.__cause__, ValueError)

    def test_evidence_write_failure_does_not_replace_recognition_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, _frame = _make_source(Path(temporary), {})
            source.analyzer.analyze.side_effect = ValueError("original analysis failure")
            source.failure_evidence.save_failure = Mock(side_effect=OSError("disk full"))
            with self.assertRaises(CampaignFailure) as raised:
                source.capture_playable_state()
            self.assertEqual("recognition_failure", raised.exception.category)
            self.assertIn("original analysis failure", raised.exception.detail)
            self.assertIn("failure_evidence_error=OSError: disk full", raised.exception.detail)

    def test_successful_observation_does_not_persist_frames(self):
        state = {"board": [[2]], "game_over": False, "state_signature": "board-one", "score": 0}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "episode-0001"
            source, _frame = _make_source(directory, state, [object()])
            result = source.capture_playable_state()
            self.assertEqual("board-one", result.signature)
            self.assertFalse(directory.exists())

    def test_failure_frame_storage_requires_explicit_cli_opt_in(self):
        for arguments, expected in (([], False), (["--save-failure-evidence"], True)):
            with self.subTest(arguments=arguments), patch.object(sys, "argv", ["run_2048_campaign", *arguments]):
                parsed = run_2048_campaign._parse_arguments()
                self.assertIs(expected, parsed.save_failure_evidence)


if __name__ == "__main__":
    unittest.main()
