import ctypes
import os
import unittest
from ctypes import wintypes

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

    def test_captured_frame_timestamps_are_monotonic(self):
        capture = WindowsScreenCapture()
        frames = [capture.capture() for _ in range(3)]

        self.assertTrue(all(earlier.captured_at <= later.captured_at for earlier, later in zip(frames, frames[1:])))

    def test_repeated_capture_releases_gdi_resources(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_gui_resources = user32.GetGuiResources
        get_gui_resources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        get_gui_resources.restype = wintypes.DWORD
        process = get_current_process()

        capture = WindowsScreenCapture()
        capture.capture()  # Warm up ctypes and GDI before taking the baseline.
        baseline = get_gui_resources(process, 0)  # GR_GDIOBJECTS
        for _ in range(12):
            capture.capture()

        self.assertEqual(get_gui_resources(process, 0), baseline)

    def test_invalid_or_lost_window_handle_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "GetWindowRect failed"):
            WindowsScreenCapture().capture(-1)

    def test_hidden_window_resize_loss_and_recreate(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        get_module_handle = kernel32.GetModuleHandleW
        get_module_handle.argtypes = [wintypes.LPCWSTR]
        get_module_handle.restype = wintypes.HINSTANCE
        create_window = user32.CreateWindowExW
        create_window.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        create_window.restype = wintypes.HWND
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        destroy_window = user32.DestroyWindow
        destroy_window.argtypes = [wintypes.HWND]

        def create_hidden_window():
            handle = create_window(
                0,
                "STATIC",
                "AI Game Player capture test",
                0x80000000,  # WS_POPUP; never shown or activated.
                10,
                10,
                180,
                120,
                None,
                None,
                get_module_handle(None),
                None,
            )
            self.assertTrue(handle, ctypes.get_last_error())
            return handle

        capture = WindowsScreenCapture()
        window = create_hidden_window()
        try:
            initial_frame = capture.capture(int(window))
            self.assertGreater(initial_frame.width, 0)
            self.assertGreater(initial_frame.height, 0)

            self.assertTrue(
                set_window_pos(window, None, 10, 10, 360, 240, 0x0004 | 0x0010),
                ctypes.get_last_error(),
            )
            resized_frame = capture.capture(int(window))
            self.assertEqual((resized_frame.width, resized_frame.height), (360, 240))
            self.assertNotEqual(
                (resized_frame.width, resized_frame.height),
                (initial_frame.width, initial_frame.height),
            )

            stale_window_handle = int(window)
            self.assertTrue(destroy_window(window), ctypes.get_last_error())
            window = None
            with self.assertRaisesRegex(RuntimeError, "GetWindowRect failed"):
                capture.capture(stale_window_handle)

            recreated_window = create_hidden_window()
            try:
                recreated_frame = capture.capture(int(recreated_window))
                self.assertGreater(recreated_frame.width, 0)
                self.assertGreater(recreated_frame.height, 0)
            finally:
                destroy_window(recreated_window)
        finally:
            if window:
                destroy_window(window)
