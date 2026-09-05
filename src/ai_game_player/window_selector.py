import ctypes
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str


class WindowsWindowSelector:
    """Enumerates visible, titled top-level windows on Windows."""

    def list_windows(self) -> list[WindowInfo]:
        if os.name != "nt":
            raise RuntimeError("WindowsWindowSelector requires Windows")
        user32 = ctypes.windll.user32
        windows: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, length + 1)
                    windows.append(WindowInfo(int(hwnd), buffer.value))
            return True

        user32.EnumWindows(callback_type(callback), 0)
        return windows