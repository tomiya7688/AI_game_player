import ctypes
import os
import unittest
from ctypes import wintypes

from ai_game_player.captured_source import CapturedObservationSource
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.window_selector import WindowsWindowSelector


@unittest.skipUnless(os.name == "nt", "requires the Windows desktop capture API")
class WindowsCaptureIntegrationTest(unittest.TestCase):
    def capture_desktop_or_skip(self, capture):
        try:
            return capture.capture()
        except RuntimeError as exc:
            if "BitBlt failed" in str(exc):
                self.skipTest("this Windows session does not expose a capturable desktop DC")
            raise

    def test_desktop_capture_produces_an_observation_without_sending_input(self):
        capture = WindowsScreenCapture()
        frame = self.capture_desktop_or_skip(capture)

        self.assertGreater(frame.width, 0)
        self.assertGreater(frame.height, 0)
        self.assertEqual(len(frame.bgra), frame.width * frame.height * 4)

        try:
            observation, candidates = CapturedObservationSource(capture, "windows-desktop").read()
        except RuntimeError as exc:
            if "BitBlt failed" in str(exc):
                self.skipTest("this Windows session does not expose a capturable desktop DC")
            raise
        self.assertEqual(observation.screen_id, "windows-desktop")
        self.assertEqual((observation.width, observation.height), (frame.width, frame.height))
        self.assertEqual(candidates, [])
        self.assertIn("mean_rgb", observation.features)

    def test_desktop_window_handle_capture_produces_a_valid_frame(self):
        desktop_window = ctypes.windll.user32.GetDesktopWindow()
        capture = WindowsScreenCapture()
        try:
            frame = capture.capture(desktop_window)
        except RuntimeError as exc:
            if "BitBlt failed" in str(exc):
                self.skipTest("this Windows session does not expose a capturable desktop DC")
            raise

        self.assertGreater(frame.width, 0)
        self.assertGreater(frame.height, 0)
        self.assertEqual(len(frame.bgra), frame.width * frame.height * 4)

    def test_captured_frame_timestamps_are_monotonic(self):
        capture = WindowsScreenCapture()
        frames = [self.capture_desktop_or_skip(capture) for _ in range(3)]

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
        self.capture_desktop_or_skip(capture)  # Warm up ctypes and GDI before taking the baseline.
        for _ in range(4):
            self.capture_desktop_or_skip(capture)
        steady_state = get_gui_resources(process, 0)  # GR_GDIOBJECTS
        for _ in range(8):
            self.capture_desktop_or_skip(capture)

        self.assertEqual(get_gui_resources(process, 0), steady_state)

    def test_invalid_or_lost_window_handle_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no longer valid"):
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
        window_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        window_class_type = type(
            "WindowClass",
            (ctypes.Structure,),
            {"_fields_": [
                ("style", wintypes.UINT),
                ("window_proc", window_proc_type),
                ("class_extra", ctypes.c_int),
                ("window_extra", ctypes.c_int),
                ("instance", wintypes.HINSTANCE),
                ("icon", wintypes.HICON),
                ("cursor", wintypes.HCURSOR),
                ("background", wintypes.HBRUSH),
                ("menu_name", wintypes.LPCWSTR),
                ("class_name", wintypes.LPCWSTR),
            ]},
        )
        register_class = user32.RegisterClassW
        register_class.argtypes = [ctypes.POINTER(window_class_type)]
        register_class.restype = wintypes.ATOM
        get_client_rect = user32.GetClientRect
        get_client_rect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        create_solid_brush = ctypes.WinDLL("gdi32", use_last_error=True).CreateSolidBrush
        create_solid_brush.argtypes = [wintypes.DWORD]
        create_solid_brush.restype = wintypes.HBRUSH
        fill_rect = user32.FillRect
        fill_rect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        delete_object = ctypes.WinDLL("gdi32", use_last_error=True).DeleteObject
        delete_object.argtypes = [wintypes.HGDIOBJ]
        delete_object.restype = wintypes.BOOL
        default_proc = user32.DefWindowProcW
        default_proc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        default_proc.restype = ctypes.c_ssize_t
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
        show_window = user32.ShowWindow
        show_window.argtypes = [wintypes.HWND, ctypes.c_int]
        update_window = user32.UpdateWindow
        update_window.argtypes = [wintypes.HWND]

        colors = {}

        def window_proc(hwnd, message, wparam, lparam):
            if message == 0x0317:  # WM_PRINT
                rect = wintypes.RECT()
                get_client_rect(hwnd, ctypes.byref(rect))
                brush = create_solid_brush(colors[int(hwnd)])
                try:
                    fill_rect(wintypes.HDC(wparam), ctypes.byref(rect), brush)
                finally:
                    delete_object(brush)
                return 1
            return default_proc(hwnd, message, wparam, lparam)

        window_proc_callback = window_proc_type(window_proc)
        class_name = "AI_GAME_PLAYER_CAPTURE_TEST"
        window_class = window_class_type()
        window_class.window_proc = window_proc_callback
        window_class.instance = get_module_handle(None)
        window_class.class_name = class_name
        atom = register_class(ctypes.byref(window_class))
        self.assertTrue(atom or ctypes.get_last_error())

        def create_hidden_window(title, color):
            handle = create_window(
                0x00000080,  # WS_EX_TOOLWINDOW; avoid a taskbar button when minimized.
                class_name,
                title,
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
            colors[int(handle)] = color
            return handle

        capture = WindowsScreenCapture()
        window = create_hidden_window("WINDOW", 0x000000FF)  # red
        second_window = create_hidden_window("WINDOW", 0x0000FF00)  # green
        try:
            # Exercise hidden HWNDs without disturbing the user's desktop.
            set_window_pos(window, None, -32000, -32000, 180, 120, 0x0004 | 0x0010)
            set_window_pos(second_window, None, -32000, -32000, 180, 120, 0x0004 | 0x0010)
            show_window(window, 4)  # SW_SHOWNOACTIVATE
            show_window(second_window, 4)
            update_window(window)
            update_window(second_window)
            visible_frame = capture.capture(int(window))
            second_visible_frame = capture.capture(int(second_window))
            first_center = (visible_frame.height // 2 * visible_frame.width + visible_frame.width // 2) * 4
            second_center = (second_visible_frame.height // 2 * second_visible_frame.width + second_visible_frame.width // 2) * 4
            self.assertEqual(visible_frame.bgra[first_center : first_center + 3], b"\x00\x00\xff")
            self.assertEqual(second_visible_frame.bgra[second_center : second_center + 3], b"\x00\xff\x00")
            show_window(window, 0)  # SW_HIDE
            show_window(second_window, 0)

            hidden_window = capture.capture(int(window))
            hidden_second_window = capture.capture(int(second_window))
            self.assertEqual(hidden_window.bgra, visible_frame.bgra)
            self.assertEqual(hidden_second_window.bgra, second_visible_frame.bgra)
            listed_handles = {item.handle for item in WindowsWindowSelector().list_windows()}
            self.assertIn(int(window), listed_handles)
            self.assertIn(int(second_window), listed_handles)

            self.assertTrue(
                set_window_pos(window, None, -32000, -32000, 360, 240, 0x0004 | 0x0010),
                ctypes.get_last_error(),
            )
            show_window(window, 4)
            update_window(window)
            resized_frame = capture.capture(int(window))
            self.assertEqual((resized_frame.width, resized_frame.height), (360, 240))
            show_window(window, 0)

            stale_window_handle = int(window)
            self.assertTrue(destroy_window(window), ctypes.get_last_error())
            window = None
            with self.assertRaisesRegex(RuntimeError, "no longer valid"):
                capture.capture(stale_window_handle)

            recreated_window = create_hidden_window("WINDOW C", 0x000000FF)
            try:
                set_window_pos(recreated_window, None, -32000, -32000, 180, 120, 0x0004 | 0x0010)
                show_window(recreated_window, 4)
                update_window(recreated_window)
                recreated_frame = capture.capture(int(recreated_window))
                self.assertGreater(recreated_frame.width, 0)
                self.assertGreater(recreated_frame.height, 0)
            finally:
                destroy_window(recreated_window)
        finally:
            if second_window:
                destroy_window(second_window)
            if window:
                destroy_window(window)
