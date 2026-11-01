import base64
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import random
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from PIL import Image
except ImportError:
    Image = None
from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.safety_guard import TargetState
from tools.run_2048_campaign import ReferenceGame2048Episode
from tools.planning_failure_header import OwnedGameFailureHeader


class OwnedGameFailureHeaderTests(unittest.TestCase):
    def setUp(self):
        self.episode = ReferenceGame2048Episode.__new__(ReferenceGame2048Episode)
        self.episode.launcher = SimpleNamespace(
            window_info=SimpleNamespace(handle=123, process_id=7),
            browser_process=SimpleNamespace(pid=7, poll=lambda: None),
        )
        self.capture = Mock(side_effect=AssertionError("must not recapture"))
        self.episode.source = SimpleNamespace(
            window_handle=123, capture=self.capture, cache=object(), lease=object()
        )
        self.source_state = dict(vars(self.episode.source))
        self.probe = Mock(
            return_value=TargetState(123, 7, True, True, True, 128, 96, 0, 0, 128, 96)
        )
        self.lines = []
        rng = random.Random(567)
        self.frame = ScreenFrame(
            128, 96, rng.randbytes(128 * 96 * 4), captured_at=1.0, frame_id=None
        )
        self.diagnostic = {
            "backend": "gdi",
            "request_started_ns": 900_000_000,
            "produced_at_ns": 1_000_000_000,
            "host_received_ns": 1_100_000_000,
        }

    def emitter(self, **values):
        return OwnedGameFailureHeader(
            enabled=values.pop("enabled", True),
            provider="planner",
            seed=481,
            write_line=values.pop("write_line", self.lines.append),
            probe=values.pop("probe", self.probe),
            **values,
        )

    def emit(self, emitter=None, **values):
        args = {
            "failed": True,
            "frame": self.frame,
            "crop_bbox": [2, 3, 100, 70],
            "capture_diagnostics": self.diagnostic,
        }
        args.update(values)
        return (emitter or self.emitter()).emit(self.episode, **args)

    @unittest.skipIf(Image is None, "optional Pillow PNG encoder is unavailable")
    def test_chunked_png_roundtrip_preserves_exact_crop_and_original_frame_binding(
        self,
    ):
        result = self.emit()
        records = [json.loads(line) for line in self.lines]
        metadata, chunks = records[0], records[1:]
        self.assertEqual(result["status"], "ready")
        self.assertGreater(len(chunks), 1)
        self.assertEqual(metadata["chunk_count"], len(chunks))
        self.assertEqual([row["index"] for row in chunks], list(range(len(chunks))))
        for row in chunks:
            self.assertEqual(row["image_id"], metadata["image_id"])
            self.assertEqual(row["count"], len(chunks))
            self.assertEqual(row["png_sha256"], metadata["png_sha256"])
            self.assertEqual(row["png_bytes"], metadata["png_bytes"])
            self.assertLessEqual(len(row["base64"]), 4096)
        raw = base64.b64decode("".join(row["base64"] for row in chunks), validate=True)
        self.assertLessEqual(len(raw), 128 * 1024)
        self.assertEqual(len(raw), metadata["png_bytes"])
        self.assertEqual(sha256(raw).hexdigest(), metadata["png_sha256"])
        self.assertEqual(sha256(self.frame.bgra).hexdigest(), metadata["frame_sha256"])
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual(image.size, (100, 70))
            offset = (3 * self.frame.width + 2) * 4
            expected = tuple(self.frame.bgra[offset + i] for i in (2, 1, 0))
            self.assertEqual(image.getpixel((0, 0)), expected)
        self.assertEqual(metadata["capture"], self.diagnostic)
        self.assertEqual(vars(self.episode.source), self.source_state)
        self.capture.assert_not_called()

    @unittest.skipIf(Image is None, "optional Pillow PNG encoder is unavailable")
    def test_default_existing_probe_uses_actual_targetstate_contract(self):
        with patch("ai_game_player.safety_guard.WindowsTargetProbe") as factory:
            factory.return_value.inspect.return_value = self.probe.return_value
            result = self.emit(self.emitter(probe=None))
        self.assertEqual(result["status"], "ready")
        factory.return_value.inspect.assert_called_once_with(123)

    def test_disabled_or_successful_episode_never_probes_encodes_or_writes(self):
        with patch.object(
            OwnedGameFailureHeader,
            "_encode_png",
            side_effect=AssertionError("must not encode"),
        ):
            self.assertEqual(
                self.emit(self.emitter(enabled=False))["status"], "disabled"
            )
            self.assertEqual(self.emit(failed=False)["status"], "not_failure")
        self.assertEqual(self.lines, [])
        self.probe.assert_not_called()
        self.capture.assert_not_called()

    @unittest.skipIf(Image is None, "optional Pillow PNG encoder is unavailable")
    def test_one_episode_emits_at_most_one_image(self):
        emitter = self.emitter()
        self.assertEqual(self.emit(emitter)["status"], "ready")
        count = len(self.lines)
        self.assertEqual(self.emit(emitter)["status"], "already_attempted")
        self.assertEqual(len(self.lines), count)
        self.probe.assert_called_once_with(123)

    def test_unverified_owner_or_product_episode_is_unavailable_without_image(self):
        for kind in ("pid", "source", "exited", "probe", "product"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "pid":
                    self.episode.launcher.window_info.process_id = 8
                elif kind == "source":
                    self.episode.source.window_handle = None
                elif kind == "exited":
                    self.episode.launcher.browser_process.poll = lambda: 0
                elif kind == "probe":
                    self.probe.return_value = replace(self.probe.return_value, pid=8)
                else:
                    self.episode = SimpleNamespace(**vars(self.episode))
                with patch.object(
                    OwnedGameFailureHeader,
                    "_encode_png",
                    side_effect=AssertionError("must not encode"),
                ):
                    result = self.emit()
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(len(self.lines), 1)
                self.assertNotIn("frame_sha256", json.loads(self.lines[0]))
                self.capture.assert_not_called()

    def test_invalid_roi_and_capture_binding_fail_closed(self):
        for changes in (
            {"crop_bbox": [0, 0, 641, 1]},
            {"crop_bbox": [0, 0, 1, 241]},
            {"crop_bbox": [127, 0, 2, 1]},
            {"crop_bbox": [0, 0, True, 1]},
            {"frame": ScreenFrame(1, 1, b"x", captured_at=1)},
            {"capture_diagnostics": dict(self.diagnostic, produced_at_ns=2)},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.emit(**changes)["status"], "unavailable")

    @unittest.skipIf(Image is None, "optional Pillow PNG encoder is unavailable")
    def test_actual_png_byte_limit_is_enforced(self):
        noisy = ScreenFrame(
            640, 240, random.Random(567).randbytes(640 * 240 * 4), captured_at=1
        )
        result = self.emit(frame=noisy, crop_bbox=[0, 0, 640, 240])
        self.assertEqual(result["reason"], "png_size_limit")
        self.assertFalse(
            any(json.loads(line)["event"].endswith("_chunk") for line in self.lines)
        )

    def test_optional_png_encoder_absence_is_unavailable(self):
        with patch.dict(sys.modules, {"PIL": None}):
            result = self.emit()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "png_encoder_unavailable")
        self.assertEqual(len(self.lines), 1)

    @unittest.skipIf(Image is None, "optional Pillow PNG encoder is unavailable")
    def test_diagnostic_future_native_timestamp_does_not_grant_or_modify_authority(
        self,
    ):
        diagnostic = dict(self.diagnostic, backend="wgc", host_received_ns=950_000_000)
        result = self.emit(capture_diagnostics=diagnostic)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["capture"]["produced_at_ns"], 1_000_000_000)
        self.assertEqual(vars(self.episode.source), self.source_state)
        self.capture.assert_not_called()

    @unittest.skipIf(Image is None, "optional Pillow PNG encoder is unavailable")
    def test_stdout_failure_does_not_raise_or_repeat_image(self):
        emitter = self.emitter(write_line=Mock(side_effect=BrokenPipeError("closed")))
        self.assertEqual(self.emit(emitter)["reason"], "stdout_unavailable")
        self.assertEqual(self.emit(emitter)["status"], "already_attempted")
        self.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
