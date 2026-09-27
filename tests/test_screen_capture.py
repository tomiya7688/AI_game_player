import os
import unittest
from unittest.mock import patch

from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture


class ScreenCaptureTest(unittest.TestCase):
    def test_frame_contract(self):
        frame = ScreenFrame(2, 3, b"x" * 24)
        self.assertEqual(len(frame.bgra), frame.width * frame.height * 4)
        self.assertGreater(frame.captured_at, 0)

    def test_frame_timestamp_uses_monotonic_clock(self):
        with patch("ai_game_player.screen_capture.time.monotonic", return_value=12.5):
            frame = ScreenFrame(1, 1, b"x" * 4)

        self.assertEqual(frame.captured_at, 12.5)

    def test_non_windows_is_explicit(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError): WindowsScreenCapture().capture()
