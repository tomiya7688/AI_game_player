"""Declare Windows ABI signatures shared by window selection, input, and capture."""

import ctypes
from ctypes import wintypes
from typing import Any


WINDOW_ENUM_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL,
    wintypes.HWND,
    wintypes.LPARAM,
)

_USER32_PROTOTYPES: dict[str, tuple[tuple[Any, ...], Any]] = {
    "EnumWindows": ((WINDOW_ENUM_CALLBACK, wintypes.LPARAM), wintypes.BOOL),
    "GetWindowTextLengthW": ((wintypes.HWND,), ctypes.c_int),
    "GetWindowTextW": ((wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
    "GetWindowThreadProcessId": (
        (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)),
        wintypes.DWORD,
    ),
    "IsWindow": ((wintypes.HWND,), wintypes.BOOL),
    "GetWindowRect": (
        (wintypes.HWND, ctypes.POINTER(ctypes.c_long * 4)),
        wintypes.BOOL,
    ),
    "ScreenToClient": (
        (wintypes.HWND, ctypes.POINTER(ctypes.c_long * 2)),
        wintypes.BOOL,
    ),
    "SetCursorPos": ((ctypes.c_int, ctypes.c_int), wintypes.BOOL),
    "PostMessageW": (
        (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
        wintypes.BOOL,
    ),
    "VkKeyScanW": ((wintypes.WCHAR,), wintypes.SHORT),
    "keybd_event": (
        (wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t),
        None,
    ),
    "mouse_event": (
        (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ),
        None,
    ),
    "GetSystemMetrics": ((ctypes.c_int,), ctypes.c_int),
    "GetDC": ((wintypes.HWND,), wintypes.HDC),
    "ReleaseDC": ((wintypes.HWND, wintypes.HDC), ctypes.c_int),
    "GetDesktopWindow": ((), wintypes.HWND),
    "IsWindowVisible": ((wintypes.HWND,), wintypes.BOOL),
    "IsIconic": ((wintypes.HWND,), wintypes.BOOL),
    "GetWindow": ((wintypes.HWND, wintypes.UINT), wintypes.HWND),
    "SendMessageTimeoutW": (
        (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ),
        wintypes.BOOL,
    ),
}

_GDI32_PROTOTYPES: dict[str, tuple[tuple[Any, ...], Any]] = {
    "CreateCompatibleDC": ((wintypes.HDC,), wintypes.HDC),
    "SelectObject": ((wintypes.HDC, wintypes.HGDIOBJ), wintypes.HGDIOBJ),
    "BitBlt": (
        (
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ),
        wintypes.BOOL,
    ),
    "DeleteObject": ((wintypes.HGDIOBJ,), wintypes.BOOL),
    "DeleteDC": ((wintypes.HDC,), wintypes.BOOL),
}


# {
#   責務: [_apply_prototypes: ctypesがC intと解釈しないようWin32関数の引数・戻り値を宣言する]
#   処理: [各ライブラリ関数がctypesのprototype属性を持つ場合だけargtypesとrestypeを設定する。Python製test doubleは変更しない]
#   引数: [api_library: WinDLLまたはPython製test double, prototypes: 関数名ごとの引数型列と戻り値型]
#   戻り値: []
# }
def _apply_prototypes(
    api_library: Any,
    prototypes: dict[str, tuple[tuple[Any, ...], Any]],
) -> None:
    for function_name, (argument_types, return_type) in prototypes.items():
        api_function = getattr(api_library, function_name, None)
        if api_function is None or not hasattr(api_function, "argtypes"):
            continue
        api_function.argtypes = list(argument_types)
        api_function.restype = return_type


# {
#   責務: [configure_user32_prototypes: window選択・入力・captureが共有するuser32関数をWindows ABIの型で宣言する]
#   処理: [HWND、WPARAM、LPARAM、pointer引数、およびBOOL・DWORD・HWNDなどの戻り値を各関数へ割り当てる]
#   引数: [user32: ctypes prototypeを設定するuser32ライブラリまたはprototype属性を持たないtest double]
#   戻り値: []
# }
def configure_user32_prototypes(user32: Any) -> None:
    _apply_prototypes(user32, _USER32_PROTOTYPES)


# {
#   責務: [configure_gdi32_prototypes: captureが使うGDI関数をWindows ABIの型で宣言する]
#   処理: [HDC・HBITMAP・HGDIOBJ・BITMAPINFO pointerと各関数のBOOL戻り値をGDI関数へ割り当てる]
#   引数: [gdi32: ctypes prototypeを設定するgdi32ライブラリまたはprototype属性を持たないtest double, bitmap_info_type: CreateDIBSectionへ渡すBITMAPINFO構造体型]
#   戻り値: []
# }
def configure_gdi32_prototypes(
    gdi32: Any,
    bitmap_info_type: type[ctypes.Structure],
) -> None:
    prototypes = {
        **_GDI32_PROTOTYPES,
        "CreateDIBSection": (
            (
                wintypes.HDC,
                ctypes.POINTER(bitmap_info_type),
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p),
                wintypes.HANDLE,
                wintypes.DWORD,
            ),
            wintypes.HBITMAP,
        ),
    }
    _apply_prototypes(gdi32, prototypes)
