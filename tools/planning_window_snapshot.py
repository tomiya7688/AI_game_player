"""比較ログ専用のread-only Win32 window geometry。入力許可やfocus操作には使わない。"""

import ctypes
from ctypes import wintypes
import os
import time
from typing import Any, Callable

from ai_game_player.safety_guard import TargetState, WindowsTargetProbe


# {
#   責務: [_configure_queries: 独立WinDLLのread-only APIだけへpointer幅を設定する]
#   引数: [api: WinDLLまたはtest double] 戻り値: [なし]
#   エラー: [AttributeError: 必要な照合/geometry APIが無い]
# }
def _configure_queries(api: Any) -> None:
    signatures = {
        "GetWindowThreadProcessId": (
            [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
            wintypes.DWORD,
        ),
        "GetWindowRect": (
            [wintypes.HWND, ctypes.POINTER(ctypes.c_long * 4)],
            wintypes.BOOL,
        ),
        "GetClientRect": (
            [wintypes.HWND, ctypes.POINTER(ctypes.c_long * 4)],
            wintypes.BOOL,
        ),
        "ClientToScreen": (
            [wintypes.HWND, ctypes.POINTER(ctypes.c_long * 2)],
            wintypes.BOOL,
        ),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "IsIconic": ([wintypes.HWND], wintypes.BOOL),
        "GetForegroundWindow": ([], wintypes.HWND),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        if hasattr(function, "argtypes"):
            function.argtypes, function.restype = arguments, result
    dpi = getattr(api, "GetDpiForWindow", None)
    if dpi is not None and hasattr(dpi, "argtypes"):
        dpi.argtypes, dpi.restype = [wintypes.HWND], wintypes.UINT


# {
#   責務: [_pid: HWNDの現所有者をread-onlyで調べる]
#   引数: [api: user32 query, handle: 選択またはforeground HWND]
#   戻り値: [int | None: 読取不能ならNone]
# }
def _pid(api: Any, handle: int) -> int | None:
    result = wintypes.DWORD()
    if (
        not api.GetWindowThreadProcessId(handle, ctypes.byref(result))
        or result.value <= 0
    ):
        return None
    return int(result.value)


# {
#   責務: [selected_window_snapshot: 既存Probeの実TargetStateと現在の座標/focusだけを診断する]
#   引数: [expected_handle/expected_pid: 専用launcherが所有する選択window,
#     frame_size: 同captureのpixel寸法, probe: read-only照合, user32: 独立WinDLLのtest seam]
#   戻り値: [dict: available/unavailableの診断。Source/Safety許可へ流用しない]
#   エラー: [照合/読取失敗はunavailable。title/desktop画像/focus変更を取得しない]
# }
def selected_window_snapshot(
    expected_handle: int,
    expected_pid: int,
    *,
    frame_size: tuple[int, int] | None = None,
    probe: Callable[[int], TargetState] | None = None,
    user32: Any | None = None,
) -> dict[str, Any]:
    started_ns = time.monotonic_ns()
    unavailable = {
        "status": "unavailable",
        "diagnostic_only": True,
        "snapshot_started_ns": started_ns,
    }
    if (
        type(expected_handle) is not int
        or not 0 < expected_handle <= 0xFFFFFFFFFFFFFFFF
        or type(expected_pid) is not int
        or not 0 < expected_pid <= 0xFFFFFFFF
    ):
        return {**unavailable, "reason": "invalid_expected_identity"}
    if frame_size is not None and (
        type(frame_size) is not tuple
        or len(frame_size) != 2
        or any(type(v) is not int or not 0 < v <= 32768 for v in frame_size)
    ):
        return {**unavailable, "reason": "invalid_frame_size"}
    if user32 is None and os.name != "nt":
        return {**unavailable, "reason": "windows_unavailable"}
    try:
        # 独立WinDLLを使い、診断の型宣言で既存SafetyProbeのfunction objectを書き換えない。
        api = (
            user32
            if user32 is not None
            else getattr(ctypes, "WinDLL")("user32", use_last_error=True)
        )
        _configure_queries(api)
        target = (probe or WindowsTargetProbe().inspect)(expected_handle)
        if (
            not isinstance(target, TargetState)
            or target.valid is not True
            or target.handle != expected_handle
            or target.pid != expected_pid
        ):
            return {**unavailable, "reason": "ownership_unverified"}
        rectangle, client = (ctypes.c_long * 4)(), (ctypes.c_long * 4)()
        origin = (ctypes.c_long * 2)(0, 0)
        if (
            not api.GetWindowRect(expected_handle, ctypes.byref(rectangle))
            or not api.GetClientRect(expected_handle, ctypes.byref(client))
            or not api.ClientToScreen(expected_handle, ctypes.byref(origin))
        ):
            return {**unavailable, "reason": "geometry_query_failed"}
        foreground = api.GetForegroundWindow()
        foreground_handle = int(foreground) if foreground else None
        foreground_pid = (
            _pid(api, foreground_handle) if foreground_handle is not None else None
        )
        dpi_function = getattr(api, "GetDpiForWindow", None)
        dpi = int(dpi_function(expected_handle)) if dpi_function is not None else 0
        # 読取の途中でhandleの所有者が変わった場合も別windowの診断として公開しない。
        if _pid(api, expected_handle) != expected_pid:
            return {**unavailable, "reason": "ownership_changed"}
        return {
            "status": "available",
            "diagnostic_only": True,
            "scope": "selected_owned_window_geometry_only",
            "snapshot_started_ns": started_ns,
            "snapshot_finished_ns": time.monotonic_ns(),
            "selected_hwnd": expected_handle,
            "selected_pid": expected_pid,
            "frame_size": list(frame_size) if frame_size is not None else None,
            "window_rect_ltrb": list(rectangle),
            "client_rect_ltrb": list(client),
            "client_screen_origin": list(origin),
            "dpi": dpi if dpi > 0 else None,
            "visible": bool(api.IsWindowVisible(expected_handle)),
            "iconic": bool(api.IsIconic(expected_handle)),
            "foreground_hwnd": foreground_handle,
            "foreground_pid": foreground_pid,
            "foreground_is_selected": foreground_handle == expected_handle,
            "foreground_pid_is_selected": foreground_pid == expected_pid
            if foreground_pid is not None
            else False,
        }
    except Exception:
        return {**unavailable, "reason": "snapshot_query_failed"}
