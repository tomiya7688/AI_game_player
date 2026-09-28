import os
import unittest
from unittest.mock import patch

from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture


class FakeUser32:
    def __init__(self, *, screen_dc=17):
        self.screen_dc = screen_dc
        self.released = []

    def GetSystemMetrics(self, index):
        return 2

    def GetDC(self, window):
        return self.screen_dc

    def ReleaseDC(self, window, device_context):
        self.released.append((window, device_context))
        return 1


class FakeGdi32:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []
        self.select_count = 0

    def CreateCompatibleDC(self, screen_dc):
        self.calls.append("CreateCompatibleDC")
        return 0 if self.failure == "CreateCompatibleDC" else 23

    def CreateCompatibleBitmap(self, screen_dc, width, height):
        self.calls.append("CreateCompatibleBitmap")
        return 0 if self.failure == "CreateCompatibleBitmap" else 29

    def SelectObject(self, memory_dc, selected_object):
        self.calls.append(("SelectObject", selected_object))
        self.select_count += 1
        if self.failure == "SelectObject":
            return 0
        return 41 if self.select_count == 1 else 29

    def BitBlt(self, *args):
        self.calls.append("BitBlt")
        return self.failure != "BitBlt"

    def GetDIBits(self, *args):
        self.calls.append("GetDIBits")
        return 0 if self.failure == "GetDIBits" else 1

    def DeleteObject(self, bitmap):
        self.calls.append("DeleteObject")
        return 1

    def DeleteDC(self, memory_dc):
        self.calls.append("DeleteDC")
        return 1


class ScreenCaptureTest(unittest.TestCase):
    def test_frame_contract(self):
        frame = ScreenFrame(2, 3, b"x" * 24)
        self.assertEqual(len(frame.bgra), frame.width * frame.height * 4)
        self.assertGreater(frame.captured_at, 0)

    def test_frame_timestamp_uses_monotonic_clock(self):
        with patch("ai_game_player.screen_capture.time.monotonic", return_value=12.5):
            frame = ScreenFrame(1, 1, b"x" * 4)

        self.assertEqual(frame.captured_at, 12.5)

    def test_get_dc_failure_stops_before_gdi_allocation(self):
        user32 = FakeUser32(screen_dc=0)
        gdi32 = FakeGdi32()

        with self.assertRaisesRegex(RuntimeError, "GetDC failed"):
            WindowsScreenCapture(user32=user32, gdi32=gdi32).capture()

        self.assertEqual(gdi32.calls, [])
        self.assertEqual(user32.released, [])

    def test_partial_gdi_allocations_are_released_after_api_failures(self):
        cases = (
            ("CreateCompatibleDC", "CreateCompatibleDC failed", False, False),
            ("CreateCompatibleBitmap", "CreateCompatibleBitmap failed", True, False),
            ("SelectObject", "SelectObject failed", True, True),
            ("BitBlt", "BitBlt failed", True, True),
            ("GetDIBits", "GetDIBits failed", True, True),
        )
        for failure, message, should_delete_dc, should_delete_bitmap in cases:
            with self.subTest(failure=failure):
                user32 = FakeUser32()
                gdi32 = FakeGdi32(failure)
                with self.assertRaisesRegex(RuntimeError, message):
                    WindowsScreenCapture(user32=user32, gdi32=gdi32).capture()

                self.assertEqual(user32.released, [(0, 17)])
                self.assertEqual("DeleteDC" in gdi32.calls, should_delete_dc)
                self.assertEqual("DeleteObject" in gdi32.calls, should_delete_bitmap)
                if failure in {"BitBlt", "GetDIBits"}:
                    self.assertIn(("SelectObject", 41), gdi32.calls)

    def test_non_windows_is_explicit(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError): WindowsScreenCapture().capture()
