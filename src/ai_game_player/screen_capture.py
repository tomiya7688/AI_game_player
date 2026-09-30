import ctypes
import os
import time
from dataclasses import dataclass, field
from ctypes import wintypes
from typing import Any


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("planes", ctypes.c_uint16),
        ("bit_count", ctypes.c_uint16),
        ("compression", ctypes.c_uint32),
        ("size_image", ctypes.c_uint32),
        ("x_ppm", ctypes.c_int32),
        ("y_ppm", ctypes.c_int32),
        ("clr_used", ctypes.c_uint32),
        ("clr_important", ctypes.c_uint32),
    ]


class BitmapInfo(ctypes.Structure):
    _fields_ = [("header", BitmapInfoHeader), ("colors", ctypes.c_uint32 * 3)]


@dataclass(frozen=True)
class ScreenFrame:
    width: int
    height: int
    bgra: bytes
    captured_at: float = field(default_factory=lambda: time.monotonic())

class WindowsScreenCapture:
    """Captures the virtual desktop without coupling capture to OCR or input."""

    def __init__(self, *, user32: Any | None = None, gdi32: Any | None = None) -> None:
        if (user32 is None) != (gdi32 is None):
            raise ValueError("user32 and gdi32 must be provided together")
        self._user32 = user32
        self._gdi32 = gdi32

    def capture(self, window_handle: int | None = None) -> ScreenFrame:
        if self._user32 is None and os.name != "nt":
            raise RuntimeError("WindowsScreenCapture requires Windows")
        user32 = self._user32 or ctypes.windll.user32
        gdi32 = self._gdi32 or ctypes.windll.gdi32
        if self._user32 is None:
            user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            user32.GetSystemMetrics.restype = ctypes.c_int
            user32.GetDC.argtypes = [wintypes.HWND]
            user32.GetDC.restype = wintypes.HDC
            user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            user32.ReleaseDC.restype = ctypes.c_int
            user32.GetDesktopWindow.restype = wintypes.HWND
            user32.IsWindow.argtypes = [wintypes.HWND]
            user32.IsWindow.restype = wintypes.BOOL
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_long * 4)]
            user32.GetWindowRect.restype = wintypes.BOOL
            user32.SendMessageTimeoutW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
            ]
            user32.SendMessageTimeoutW.restype = ctypes.c_size_t
            gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
            gdi32.CreateCompatibleDC.restype = wintypes.HDC
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
            gdi32.SelectObject.restype = wintypes.HGDIOBJ
            gdi32.BitBlt.argtypes = [
                wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
            ]
            gdi32.BitBlt.restype = wintypes.BOOL
            gdi32.CreateDIBSection.argtypes = [
                wintypes.HDC, ctypes.POINTER(BitmapInfo), wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
            ]
            gdi32.CreateDIBSection.restype = wintypes.HBITMAP
            gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            gdi32.DeleteObject.restype = wintypes.BOOL
            gdi32.DeleteDC.argtypes = [wintypes.HDC]
            gdi32.DeleteDC.restype = wintypes.BOOL
        capture_window = window_handle is not None and window_handle != user32.GetDesktopWindow()
        if capture_window:
            if not user32.IsWindow(window_handle):
                raise RuntimeError("Selected window handle is no longer valid")
            rect = (ctypes.c_long * 4)()
            if not user32.GetWindowRect(window_handle, ctypes.byref(rect)):
                raise RuntimeError("GetWindowRect failed")
            left, top, right, bottom = rect
            width, height = right - left, bottom - top
        elif window_handle is None:
            left, top = 0, 0
            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)
        else:
            left, top = 0, 0
            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)
        if width <= 0 or height <= 0:
            raise RuntimeError("Capture area has no visible size")

        screen_dc = None if capture_window else user32.GetDC(0)
        if not capture_window and not screen_dc:
            raise RuntimeError("GetDC failed")
        memory_dc = None
        bitmap = None
        previous_object = None
        try:
            memory_dc = gdi32.CreateCompatibleDC(screen_dc or 0)
            if not memory_dc:
                raise RuntimeError("CreateCompatibleDC failed")
            header = BitmapInfoHeader(
                ctypes.sizeof(BitmapInfoHeader), width, -height, 1, 32, 0,
                width * height * 4, 0, 0, 0, 0,
            )
            bitmap_info = BitmapInfo(header, (ctypes.c_uint32 * 3)(0, 0, 0))
            pixel_data = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(
                screen_dc or 0, ctypes.byref(bitmap_info), 0,
                ctypes.byref(pixel_data), None, 0,
            )
            if not bitmap:
                raise RuntimeError("CreateDIBSection failed")
            previous_object = gdi32.SelectObject(memory_dc, bitmap)
            if not previous_object or previous_object in (-1, ctypes.c_void_p(-1).value):
                previous_object = None
                raise RuntimeError("SelectObject failed")
            if capture_window:
                message_result = ctypes.c_size_t()
                rendered = user32.SendMessageTimeoutW(
                    window_handle,
                    0x0317,  # WM_PRINT
                    memory_dc,
                    0x0000001E,  # PRF_NONCLIENT | PRF_CLIENT | PRF_ERASEBKGND | PRF_CHILDREN
                    0x00000003,  # SMTO_BLOCK | SMTO_ABORTIFHUNG
                    1000,
                    ctypes.byref(message_result),
                )
                if not rendered:
                    raise RuntimeError("WM_PRINT failed or timed out; the selected window did not provide its contents")
                if not user32.IsWindow(window_handle):
                    raise RuntimeError("Selected window handle was lost during capture")
            elif not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, 0x00CC0020):
                raise RuntimeError("BitBlt failed")
            if not pixel_data:
                raise RuntimeError("CreateDIBSection returned no pixel buffer")
            return ScreenFrame(width, height, ctypes.string_at(pixel_data, width * height * 4))
        finally:
            if previous_object is not None:
                gdi32.SelectObject(memory_dc, previous_object)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            if screen_dc:
                user32.ReleaseDC(0, screen_dc)
