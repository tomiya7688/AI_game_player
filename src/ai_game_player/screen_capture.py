import ctypes
import os
import time
from dataclasses import dataclass, field
from typing import Any

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
        if window_handle is None:
            left, top = 0, 0
            width=user32.GetSystemMetrics(0); height=user32.GetSystemMetrics(1)
        else:
            rect = (ctypes.c_long * 4)()
            if not user32.GetWindowRect(window_handle, ctypes.byref(rect)): raise RuntimeError("GetWindowRect failed")
            left, top, right, bottom = rect
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0: raise RuntimeError("Selected window has no visible area")
        if width <= 0 or height <= 0:
            raise RuntimeError("Capture area has no visible size")

        screen_dc = user32.GetDC(0)
        if not screen_dc:
            raise RuntimeError("GetDC failed")
        memory_dc = None
        bitmap = None
        previous_object = None
        try:
            memory_dc = gdi32.CreateCompatibleDC(screen_dc)
            if not memory_dc:
                raise RuntimeError("CreateCompatibleDC failed")
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not bitmap:
                raise RuntimeError("CreateCompatibleBitmap failed")
            previous_object = gdi32.SelectObject(memory_dc, bitmap)
            if not previous_object or previous_object == -1:
                previous_object = None
                raise RuntimeError("SelectObject failed")
            if not gdi32.BitBlt(memory_dc,0,0,width,height,screen_dc,left,top,0x00CC0020): raise RuntimeError("BitBlt failed")
            class BitmapInfoHeader(ctypes.Structure):
                _fields_=[("size",ctypes.c_uint32),("width",ctypes.c_int32),("height",ctypes.c_int32),("planes",ctypes.c_uint16),("bit_count",ctypes.c_uint16),("compression",ctypes.c_uint32),("size_image",ctypes.c_uint32),("x_ppm",ctypes.c_int32),("y_ppm",ctypes.c_int32),("clr_used",ctypes.c_uint32),("clr_important",ctypes.c_uint32)]
            header=BitmapInfoHeader(ctypes.sizeof(BitmapInfoHeader),width,-height,1,32,0,width*height*4,0,0,0,0); buffer=ctypes.create_string_buffer(width*height*4)
            if not gdi32.GetDIBits(memory_dc,bitmap,0,height,buffer,ctypes.byref(header),0): raise RuntimeError("GetDIBits failed")
            return ScreenFrame(width,height,buffer.raw)
        finally:
            if previous_object is not None:
                gdi32.SelectObject(memory_dc, previous_object)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(0, screen_dc)
