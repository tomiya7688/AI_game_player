import ctypes
import os
import time
from dataclasses import dataclass, field
from ctypes import wintypes
from collections.abc import Callable
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
# {
#   責務: [ScreenFrame: capture backendが返す寸法検証可能なBGRA pixelとcapture時刻を保持する]
#   フィールド: [width/height: pixel寸法, bgra: row paddingなしのpixel bytes, captured_at: monotonic取得時刻秒, frame_id: backendが提供する場合のsession内frame ID]
# }
class ScreenFrame:
    width: int
    height: int
    bgra: bytes
    captured_at: float = field(default_factory=lambda: time.monotonic())
    frame_id: int | None = None

# {
#   責務: [WindowsScreenCapture: 選択HWNDのBGRA frameをWGCから取得し、利用不能時は既存GDI経路へfallbackする]
#   フィールド: [native_runtime/wgc_session: 選択window captureに使うNative資源, last_backend_error: WGC fallbackの最後の理由]
# }
class WindowsScreenCapture:
    """Capture one selected window with optional WGC and a GDI compatibility path."""

    # {
    #   責務: [__init__: Windows API依存、optional WGC session生成関数、capture資源状態を初期化する]
    #   引数: [user32/gdi32: Windows APIのtest double。片方だけの注入は拒否, wgc_capture_factory: HWNDからnative capture sessionを作るoptional test seam]
    #   戻り値: []
    # }
    def __init__(
        self,
        *,
        user32: Any | None = None,
        gdi32: Any | None = None,
        wgc_capture_factory: Callable[[int], Any] | None = None,
    ) -> None:
        if (user32 is None) != (gdi32 is None):
            raise ValueError("user32 and gdi32 must be provided together")
        self._user32 = user32
        self._gdi32 = gdi32
        self._wgc_capture_factory = wgc_capture_factory
        self._native_runtime: Any | None = None
        self._wgc_session: Any | None = None
        self._wgc_window_handle: int | None = None
        self._wgc_disabled = False
        self._closed = False
        self.last_backend_error: str | None = None

    # {
    #   責務: [capture: 指定windowをWGCで取得し、WGCが使えない場合は既存GDI経路へfallbackする]
    #   引数: [window_handle: capture対象のWindows HWND、省略時はdesktop]
    #   戻り値: [ScreenFrame: packed BGRA pixelとmonotonic取得時刻を持つ画面]
    #   エラー: [無効HWND・GDI失敗・capture area不正: callerが次の操作を選べるよう説明付きで送出]
    # }
    def capture(self, window_handle: int | None = None) -> ScreenFrame:
        if self._closed:
            raise RuntimeError("WindowsScreenCapture is closed and cannot capture another frame")
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
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetWindow.restype = wintypes.HWND
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
            wgc_frame = self._capture_with_wgc(int(window_handle))
            if wgc_frame is not None:
                return wgc_frame
        use_desktop_pixels = not capture_window
        if capture_window:
            if not user32.IsWindow(window_handle):
                raise RuntimeError("Selected window handle is no longer valid")
            rect = (ctypes.c_long * 4)()
            if not user32.GetWindowRect(window_handle, ctypes.byref(rect)):
                raise RuntimeError("GetWindowRect failed")
            left, top, right, bottom = rect
            width, height = right - left, bottom - top
            use_desktop_pixels = self._is_visible_and_unoccluded(
                user32, window_handle, left, top, right, bottom,
            )
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

        screen_dc = user32.GetDC(0) if use_desktop_pixels else None
        if use_desktop_pixels and not screen_dc:
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
            if capture_window and not use_desktop_pixels:
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

    # {
    #   責務: [_capture_with_wgc: 同じHWNDのWGC sessionを再利用してnative frameを取得する]
    #   引数: [window_handle: IsWindowで有効性を確認したcapture対象HWND]
    #   戻り値: [ScreenFrame | None: WGC frame、またはGDI fallbackを使う場合のNone]
    #   エラー: [WGC開始・取得・metadata失敗: 原因をlast_backend_errorへ残しsessionを閉じてNoneを返す]
    # }
    def _capture_with_wgc(self, window_handle: int) -> ScreenFrame | None:
        if self._user32 is not None and self._wgc_capture_factory is None:
            return None
        if self._wgc_window_handle != window_handle:
            if not self._close_wgc_session():
                self._wgc_disabled = True
                return None
            self._wgc_window_handle = window_handle
            self._wgc_disabled = False
        if self._wgc_disabled:
            return None
        try:
            if self._wgc_session is None:
                if self._wgc_capture_factory is not None:
                    self._wgc_session = self._wgc_capture_factory(window_handle)
                elif self._native_runtime is None:
                    from ai_game_player.runtime.native import NativeLoadStatus, discover_native_runtime

                    load_result = discover_native_runtime()
                    if load_result.status != NativeLoadStatus.AVAILABLE or load_result.runtime is None:
                        self.last_backend_error = load_result.reason or "Native Runtime is unavailable"
                        self._wgc_disabled = True
                        return None
                    self._native_runtime = load_result.runtime
                    if not self._native_runtime.is_wgc_capture_supported():
                        self.last_backend_error = "Windows Graphics Capture is not supported by this Windows runtime"
                        self._wgc_disabled = True
                        return None
                    self._wgc_session = self._native_runtime.open_wgc_window(window_handle)
                else:
                    if not self._native_runtime.is_wgc_capture_supported():
                        self.last_backend_error = "Windows Graphics Capture is not supported by this Windows runtime"
                        self._wgc_disabled = True
                        return None
                    self._wgc_session = self._native_runtime.open_wgc_window(window_handle)
            frame = self._wgc_session.capture()
            if (
                not isinstance(frame, ScreenFrame)
                or frame.width <= 0
                or frame.height <= 0
                or len(frame.bgra) != frame.width * frame.height * 4
                or frame.captured_at <= 0
            ):
                raise RuntimeError("WGC returned a frame that violates the packed BGRA contract")
            self.last_backend_error = None
            return frame
        except Exception as error:
            self.last_backend_error = f"{type(error).__name__}: {error}"
            self._close_wgc_session()
            self._wgc_disabled = True
            return None

    # {
    #   責務: [_close_wgc_session: 現在のwindow専用WGC sessionを終了しnative texture資源を解放する]
    #   戻り値: [bool: sessionが解放済み、またはclose不要の場合はTrue]
    #   エラー: [session close失敗: GDI fallbackを継続し、原因をlast_backend_errorへ記録]
    # }
    def _close_wgc_session(self) -> bool:
        session = self._wgc_session
        if session is None:
            return True
        try:
            session.close()
        except Exception as error:
            self.last_backend_error = f"{type(error).__name__}: {error}"
            return False
        self._wgc_session = None
        return True

    # {
    #   責務: [close: WGC sessionとこのcapture objectが開いたNative Runtimeを終了する]
    #   戻り値: []
    #   エラー: [Native Runtime close失敗: callerがshutdown failureを認識できるよう送出]
    # }
    def close(self) -> None:
        if self._closed:
            return
        if not self._close_wgc_session():
            raise RuntimeError(f"WGC session could not be closed: {self.last_backend_error}")
        runtime = self._native_runtime
        if runtime is not None:
            runtime.close()
            self._native_runtime = None
        self._closed = True

    @staticmethod
    def _is_visible_and_unoccluded(user32: Any, window: int, left: int, top: int, right: int, bottom: int) -> bool:
        if not user32.IsWindowVisible(window) or user32.IsIconic(window):
            return False
        if left < 0 or top < 0 or right > user32.GetSystemMetrics(0) or bottom > user32.GetSystemMetrics(1):
            return False

        above = user32.GetWindow(window, 3)  # GW_HWNDPREV: windows above the target in z-order.
        visited: set[int] = set()
        while above:
            above_handle = int(above)
            if above_handle in visited:
                return False
            visited.add(above_handle)
            if user32.IsWindowVisible(above):
                rect = (ctypes.c_long * 4)()
                if not user32.GetWindowRect(above, ctypes.byref(rect)):
                    return False
                other_left, other_top, other_right, other_bottom = rect
                if left < other_right and other_left < right and top < other_bottom and other_top < bottom:
                    return False
            above = user32.GetWindow(above, 3)
        return True
