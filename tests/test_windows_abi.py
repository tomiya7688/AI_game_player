import ast
import ctypes
import inspect
import os
import unittest
from ctypes import wintypes
from types import SimpleNamespace

from ai_game_player.screen_capture import BitmapInfo
from ai_game_player import screen_capture, window_selector, windows_input
from ai_game_player.windows_abi import (
    WINDOW_ENUM_CALLBACK,
    _GDI32_PROTOTYPES,
    _USER32_PROTOTYPES,
    configure_gdi32_prototypes,
    configure_user32_prototypes,
)


def make_native_stub():
    return ctypes.CFUNCTYPE(ctypes.c_int)(lambda: 1)


class WindowsAbiTest(unittest.TestCase):
    def test_every_scoped_win32_call_has_a_declared_prototype(self):
        user32_calls = set()
        gdi32_calls = set()
        for source_module in (screen_capture, window_selector, windows_input):
            syntax_tree = ast.parse(inspect.getsource(source_module))
            for node in ast.walk(syntax_tree):
                if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                    continue
                if node.value.id == "user32":
                    user32_calls.add(node.attr)
                elif node.value.id == "gdi32":
                    gdi32_calls.add(node.attr)

        self.assertEqual(user32_calls - set(_USER32_PROTOTYPES), set())
        self.assertEqual(gdi32_calls - set(_GDI32_PROTOTYPES) - {"CreateDIBSection"}, set())

    @unittest.skipUnless(os.name == "nt", "native Win32 DLLs are available only on Windows")
    def test_native_libraries_accept_prototypes_without_sending_input(self):
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        configure_user32_prototypes(user32)
        configure_gdi32_prototypes(gdi32, BitmapInfo)

        self.assertEqual(len(user32.PostMessageW.argtypes), 4)
        self.assertIs(user32.keybd_event.restype, None)
        self.assertIs(user32.SendMessageTimeoutW.restype, wintypes.BOOL)
        self.assertIs(gdi32.CreateDIBSection.restype, wintypes.HBITMAP)
        self.assertIs(gdi32.SelectObject.restype, wintypes.HGDIOBJ)

    def test_user32_window_enumeration_uses_windows_callback_and_bool_return(self):
        user32 = SimpleNamespace(EnumWindows=make_native_stub())

        configure_user32_prototypes(user32)

        self.assertEqual(user32.EnumWindows.argtypes, [WINDOW_ENUM_CALLBACK, wintypes.LPARAM])
        self.assertIs(user32.EnumWindows.restype, wintypes.BOOL)
        self.assertIs(WINDOW_ENUM_CALLBACK._restype_, wintypes.BOOL)
        if os.name == "nt":
            self.assertEqual(ctypes.sizeof(WINDOW_ENUM_CALLBACK._restype_), 4)

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit HWND requires a 64-bit process")
    def test_user32_hwnd_argument_preserves_values_above_32_bits(self):
        received_handles = []
        is_window = ctypes.CFUNCTYPE(wintypes.BOOL, wintypes.HWND)(
            lambda handle: received_handles.append(handle) or 1
        )
        user32 = SimpleNamespace(IsWindow=is_window)
        wide_handle = 0x1_2345_6789

        configure_user32_prototypes(user32)

        self.assertTrue(user32.IsWindow(wide_handle))
        self.assertEqual(received_handles, [wide_handle])

    def test_gdi_prototypes_declare_bitmap_and_pointer_width_outputs(self):
        gdi32 = SimpleNamespace(
            CreateCompatibleDC=make_native_stub(),
            SelectObject=make_native_stub(),
            BitBlt=make_native_stub(),
            CreateDIBSection=make_native_stub(),
            DeleteObject=make_native_stub(),
            DeleteDC=make_native_stub(),
        )

        configure_gdi32_prototypes(gdi32, BitmapInfo)

        self.assertIs(gdi32.CreateDIBSection.restype, wintypes.HBITMAP)
        self.assertIs(gdi32.CreateDIBSection.argtypes[1], ctypes.POINTER(BitmapInfo))
        self.assertIs(
            gdi32.CreateDIBSection.argtypes[3],
            ctypes.POINTER(ctypes.c_void_p),
        )
        self.assertIs(gdi32.SelectObject.restype, wintypes.HGDIOBJ)
        self.assertIs(gdi32.BitBlt.restype, wintypes.BOOL)
        self.assertIs(gdi32.DeleteDC.restype, wintypes.BOOL)


if __name__ == "__main__":
    unittest.main()
