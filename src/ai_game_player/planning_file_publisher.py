"""Planning IPC専用: open readerを旧snapshotへ保ったまま原子的に公開する。"""

import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
from typing import Any


# {
#   責務: [_RenameInfoEx: Windows FILE_RENAME_INFOの可変長尾部位置を定義]
#   フィールド: [Flags: rename bitmask, RootDirectory: NULLで絶対path,
#     FileNameLength: NULを除くUTF-16 byte長, FileName: 可変長UTF-16尾部の開始]
# }
class _RenameInfoEx(ctypes.Structure):
    """Windows FILE_RENAME_INFOの可変長UTF-16尾部直前までのABI。"""

    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_uint16 * 1),
    ]


# {
#   責務: [replace_planning_file: fsync済み同directoryのplanning messageを原子的に公開]
#   処理: [非Windowsは既存os.replace。WindowsはPOSIX rename semanticsで旧readerを維持]
#   引数: [source: 準備済み一時file, target: planner所有通信先]
#   戻り値: [なし] エラー: [OSError: unsupported/権限/IO失敗。fallbackやretry無し]
# }
def replace_planning_file(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return
    _replace_windows_planning_file(source, target)


# {
#   責務: [_rename_buffer: FILE_RENAME_INFO_EX用にUTF-16 byte長とplatform alignmentを保持]
#   処理: [Flags=REPLACE_IF_EXISTS|POSIX_SEMANTICS、絶対path、終端NULはlengthに含めない]
#   引数: [target: 置換先] 戻り値: [ctypes buffer: API呼出し完了まで所有]
#   エラー: [ValueError: NUL/過大path, UnicodeError: encode失敗]
# }
def _rename_buffer(target: Path) -> Any:
    path = os.path.abspath(target)
    if "\0" in path:
        raise ValueError("planning destination contains NUL")
    encoded = path.encode("utf-16-le")
    if len(encoded) > 2 * 32767:
        raise ValueError("planning destination is too long")
    size = max(
        ctypes.sizeof(_RenameInfoEx), _RenameInfoEx.FileName.offset + len(encoded) + 2
    )
    if size > 0xFFFFFFFF:
        raise ValueError("planning rename buffer is too long")
    buffer = ctypes.create_string_buffer(size)
    header = _RenameInfoEx.from_buffer(buffer)
    header.Flags = 0x1 | 0x2
    header.RootDirectory = None
    header.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + _RenameInfoEx.FileName.offset, encoded, len(encoded)
    )
    return buffer


# {
#   責務: [_replace_windows_planning_file: DELETE handleで単一native atomic renameを行う]
#   処理: [FileRenameInfoEx=22/Flags3。rename失敗は旧targetを維持しhandleを一度だけclose。
#     rename成功後のclose失敗は新target公開済みでもOSErrorで通知]
#   引数: [source: 一時file, target: 最終path] 戻り値: [なし]
#   エラー: [OSError: open/rename/close/API未対応。先行deleteや非atomic fallback無し]
# }
def _replace_windows_planning_file(source: Path, target: Path) -> None:
    kernel = _windows_publish_api()
    win_error = getattr(ctypes, "WinError")
    last_error = getattr(ctypes, "get_last_error")
    buffer = _rename_buffer(target)
    handle = kernel.CreateFileW(str(source), 0x00010000, 7, None, 3, 0x80, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise win_error(last_error())
    try:
        if not kernel.SetFileInformationByHandle(
            handle, 22, buffer, ctypes.sizeof(buffer)
        ):
            raise win_error(last_error())
    except BaseException:
        # Preserve the rename failure; close is still attempted exactly once.
        kernel.CloseHandle(handle)
        raise
    else:
        if not kernel.CloseHandle(handle):
            raise win_error(last_error())


# {
#   責務: [_windows_publish_api: planning publisherだけのWin32 ABIを一度解決]
#   処理: [DLL/ABIのみcacheし、file handleやbufferはcacheしない]
#   引数: [なし] 戻り値: [DLL binding] エラー: [OSError: API未対応/解決失敗]
# }
@lru_cache(maxsize=1)
def _windows_publish_api() -> Any:
    from ctypes import wintypes

    try:
        kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel.SetFileInformationByHandle.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        return kernel
    except AttributeError as error:
        raise OSError(
            errno.ENOSYS, "Windows planning rename API is unavailable"
        ) from error
