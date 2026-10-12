import ctypes
import os
import unittest
from unittest.mock import patch

from ai_game_player.capture_region import CaptureRegion, CaptureRegionMode, NormalizedRect, PixelRect
from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture


class FakeUser32:
    def __init__(self, *, screen_dc=17, window_visible=False, window_above=0):
        self.screen_dc = screen_dc
        self.window_visible = window_visible
        self.window_above = window_above
        self.released = []
        self.send_message_result = 1

    def GetSystemMetrics(self, index):
        return 100

    def GetDesktopWindow(self):
        return 999

    def IsWindow(self, window):
        return window == 123

    def IsWindowVisible(self, window):
        return self.window_visible

    def IsIconic(self, window):
        return False

    def GetWindow(self, window, command):
        return self.window_above if window == 123 else 0

    def GetWindowRect(self, window, rect_pointer):
        values = ctypes.cast(rect_pointer, ctypes.POINTER(ctypes.c_long * 4)).contents
        values[:] = (10, 20, 12, 22)
        return 1

    def SendMessageTimeoutW(self, window, message, device_context, flags, send_flags, timeout, result_pointer):
        self.send_message_args = (window, message, device_context, flags, send_flags, timeout)
        return self.send_message_result

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

    def SelectObject(self, memory_dc, selected_object):
        self.calls.append(("SelectObject", selected_object))
        self.select_count += 1
        if self.failure == "SelectObject":
            return 0
        return 41 if self.select_count == 1 else 29

    def BitBlt(self, *args):
        self.calls.append("BitBlt")
        return self.failure != "BitBlt"

    def CreateDIBSection(self, device_context, bitmap_info, usage, pixel_pointer, section, offset):
        self.calls.append("CreateDIBSection")
        if self.failure == "CreateDIBSection":
            return 0
        width = bitmap_info._obj.header.width
        height = abs(bitmap_info._obj.header.height)
        self.pixel_buffer = ctypes.create_string_buffer(width * height * 4)
        pixels = ctypes.cast(self.pixel_buffer, ctypes.c_void_p)
        ctypes.cast(pixel_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = pixels
        return 29

    def DeleteObject(self, bitmap):
        self.calls.append("DeleteObject")
        return 1

    def DeleteDC(self, memory_dc):
        self.calls.append("DeleteDC")
        return 1


class FakeWgcCapture:
    def __init__(self, frame=None, error=None):
        self.frame = frame or ScreenFrame(2, 2, b"w" * 16, captured_at=12.0, frame_id=4)
        self.error = error
        self.closed = False

    def capture(self):
        if self.error is not None:
            raise self.error
        return self.frame

    def close(self):
        self.closed = True


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

    def test_window_capture_uses_selected_window_not_desktop_pixels(self):
        user32 = FakeUser32()
        gdi32 = FakeGdi32()

        frame = WindowsScreenCapture(user32=user32, gdi32=gdi32).capture(123)

        self.assertEqual((frame.width, frame.height), (2, 2))
        self.assertEqual(user32.send_message_args, (123, 0x0317, 23, 0x1E, 0x3, 1000))
        self.assertNotIn("BitBlt", gdi32.calls)

    def test_window_capture_prefers_native_wgc_frame_when_available(self):
        user32 = FakeUser32()
        gdi32 = FakeGdi32()
        native_frame = ScreenFrame(2, 2, b"n" * 16, captured_at=14.0, frame_id=9)
        wgc_capture = FakeWgcCapture(native_frame)
        factory_calls = []

        capture = WindowsScreenCapture(
            user32=user32,
            gdi32=gdi32,
            wgc_capture_factory=lambda handle: factory_calls.append(handle) or wgc_capture,
        )
        frame = capture.capture(123)

        self.assertIs(native_frame, frame)
        self.assertEqual([123], factory_calls)
        self.assertEqual([], gdi32.calls)
        capture.close()
        self.assertTrue(wgc_capture.closed)

    def test_window_relative_mode_crops_the_selected_window_and_keeps_screen_mapping(self):
        user32 = FakeUser32()
        gdi32 = FakeGdi32()
        native_frame = ScreenFrame(4, 2, b"n" * 32, captured_at=14.0, frame_id=9)
        capture = WindowsScreenCapture(
            user32=user32,
            gdi32=gdi32,
            wgc_capture_factory=lambda _handle: FakeWgcCapture(native_frame),
        )
        capture.set_capture_region(
            CaptureRegion(
                CaptureRegionMode.WINDOW_RELATIVE_REGION,
                NormalizedRect(0.5, 0, 0.5, 1),
            )
        )

        frame = capture.capture(123)

        self.assertEqual((frame.width, frame.height, len(frame.bgra)), (2, 2, 16))
        self.assertEqual(frame.capture_geometry.crop_rect, PixelRect(2, 0, 2, 2))
        self.assertEqual(frame.capture_geometry.roi_to_window(0, 0), (1.0, 0.0))
        self.assertEqual(frame.capture_geometry.roi_to_screen(0, 0), (11.0, 20.0))
        self.assertEqual(gdi32.calls, [])
        capture.close()

    def test_fixed_region_captures_desktop_pixels_and_maps_back_to_selected_window(self):
        user32 = FakeUser32()
        gdi32 = FakeGdi32()
        capture = WindowsScreenCapture(user32=user32, gdi32=gdi32)
        capture.set_capture_region(
            CaptureRegion(CaptureRegionMode.FIXED_REGION, PixelRect(10, 20, 2, 2))
        )

        frame = capture.capture(123)

        self.assertEqual((frame.width, frame.height, len(frame.bgra)), (2, 2, 16))
        self.assertEqual(frame.capture_geometry.roi_to_window(0, 0), (0.0, 0.0))
        self.assertEqual(frame.capture_geometry.roi_to_screen(0, 0), (10.0, 20.0))
        self.assertEqual(frame.capture_geometry.source, "configured:fixed_region")
        self.assertIn("BitBlt", gdi32.calls)

    def test_window_change_replaces_the_wgc_session_after_reusing_the_current_window(self):
        user32 = FakeUser32()
        user32.IsWindow = lambda handle: handle in (123, 124)
        gdi32 = FakeGdi32()
        first_session = FakeWgcCapture(ScreenFrame(2, 2, b"a" * 16, captured_at=14.0, frame_id=1))
        second_session = FakeWgcCapture(ScreenFrame(2, 2, b"b" * 16, captured_at=15.0, frame_id=1))
        sessions = {123: first_session, 124: second_session}
        factory_calls = []
        capture = WindowsScreenCapture(
            user32=user32,
            gdi32=gdi32,
            wgc_capture_factory=lambda handle: factory_calls.append(handle) or sessions[handle],
        )

        self.assertIs(first_session.frame, capture.capture(123))
        self.assertIs(first_session.frame, capture.capture(123))
        self.assertIs(second_session.frame, capture.capture(124))

        self.assertEqual([123, 124], factory_calls)
        self.assertTrue(first_session.closed)
        self.assertFalse(second_session.closed)
        self.assertEqual([], gdi32.calls)
        capture.close()
        self.assertTrue(second_session.closed)

    def test_window_capture_falls_back_to_gdi_after_native_wgc_failure(self):
        user32 = FakeUser32()
        gdi32 = FakeGdi32()
        wgc_capture = FakeWgcCapture(error=RuntimeError("WGC device was lost"))
        capture = WindowsScreenCapture(
            user32=user32,
            gdi32=gdi32,
            wgc_capture_factory=lambda _handle: wgc_capture,
        )

        frame = capture.capture(123)

        self.assertEqual((2, 2), (frame.width, frame.height))
        self.assertIn("WGC device was lost", capture.last_backend_error)
        self.assertTrue(wgc_capture.closed)
        self.assertIn("CreateDIBSection", gdi32.calls)

    def test_window_capture_fails_closed_when_window_does_not_render(self):
        user32 = FakeUser32()
        user32.send_message_result = 0
        gdi32 = FakeGdi32()

        with self.assertRaisesRegex(RuntimeError, "WM_PRINT failed or timed out"):
            WindowsScreenCapture(user32=user32, gdi32=gdi32).capture(123)

        self.assertNotIn("BitBlt", gdi32.calls)

    def test_visible_unoccluded_window_uses_its_screen_region(self):
        user32 = FakeUser32(window_visible=True)
        gdi32 = FakeGdi32()

        WindowsScreenCapture(user32=user32, gdi32=gdi32).capture(123)

        self.assertIn("BitBlt", gdi32.calls)
        self.assertFalse(hasattr(user32, "send_message_args"))

    def test_occluded_window_never_falls_back_to_other_windows_pixels(self):
        user32 = FakeUser32(window_visible=True, window_above=456)
        gdi32 = FakeGdi32()

        WindowsScreenCapture(user32=user32, gdi32=gdi32).capture(123)

        self.assertNotIn("BitBlt", gdi32.calls)
        self.assertEqual(user32.send_message_args[0], 123)

    def test_invalid_window_is_rejected_before_capture(self):
        user32 = FakeUser32()
        gdi32 = FakeGdi32()

        with self.assertRaisesRegex(RuntimeError, "no longer valid"):
            WindowsScreenCapture(user32=user32, gdi32=gdi32).capture(456)

        self.assertEqual(gdi32.calls, [])

    def test_partial_gdi_allocations_are_released_after_api_failures(self):
        cases = (
            ("CreateCompatibleDC", "CreateCompatibleDC failed", False, False),
            ("CreateDIBSection", "CreateDIBSection failed", True, False),
            ("SelectObject", "SelectObject failed", True, True),
            ("BitBlt", "BitBlt failed", True, True),
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
                if failure == "BitBlt":
                    self.assertIn(("SelectObject", 41), gdi32.calls)

    def test_non_windows_is_explicit(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError): WindowsScreenCapture().capture()
