import ctypes
import os
import unittest

from ai_game_player.captured_source import CapturedObservationSource
from ai_game_player.screen_capture import WindowsScreenCapture


@unittest.skipUnless(os.name == "nt", "requires the Windows desktop capture API")
class WindowsCaptureIntegrationTest(unittest.TestCase):
    def test_desktop_capture_produces_an_observation_without_sending_input(self):
        capture = WindowsScreenCapture()
        frame = capture.capture()

        self.assertGreater(frame.width, 0)
        self.assertGreater(frame.height, 0)
        self.assertEqual(len(frame.bgra), frame.width * frame.height * 4)

        observation, candidates = CapturedObservationSource(capture, "windows-desktop").read()
        self.assertEqual(observation.screen_id, "windows-desktop")
        self.assertEqual((observation.width, observation.height), (frame.width, frame.height))
        self.assertEqual(candidates, [])
        self.assertIn("mean_rgb", observation.features)

    def test_desktop_window_handle_capture_produces_a_valid_frame(self):
        desktop_window = ctypes.windll.user32.GetDesktopWindow()
        frame = WindowsScreenCapture().capture(desktop_window)

        self.assertGreater(frame.width, 0)
        self.assertGreater(frame.height, 0)
        self.assertEqual(len(frame.bgra), frame.width * frame.height * 4)
