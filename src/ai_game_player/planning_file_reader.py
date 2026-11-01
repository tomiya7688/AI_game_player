"""atomic replaceを妨げず、planner所有messageだけをbyte上限付きで読む。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
import os
from pathlib import Path
from typing import Any, BinaryIO


# {
#   責務: [read_planning_bytes: JSONとraw checkpoint検査で同じ有界readerを使う]
#   処理: [最大+1byteのみ読み、上限超過と不正な予算を拒否する。再試行はしない]
#   引数: [path: planner所有の一時file, maximum: 非負整数のbyte上限]
#   戻り値: [bytes: 上限内の独立したfile内容]
#   エラー: [OSError: open/read/close失敗, ValueError: 予算が不正またはfileが上限超過]
# }
def read_planning_bytes(path: Path, maximum: int) -> bytes:
    if type(maximum) is not int or maximum < 0:
        raise ValueError("planning message byte budget must be a nonnegative integer")
    with _open_planning_reader(path) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("planning message byte budget exceeded")
    return raw


# {
#   責務: [_open_planning_reader: platformに合う読取handleの寿命を一つのcontextへ閉じる]
#   処理: [WindowsはDELETE共有を許し、他OSは既存のPath.openを維持する]
#   引数: [path: planner所有の一時file]
#   戻り値: [BinaryIOのcontext。呼出側はread量を制限する]
#   エラー: [OSError: open/read/close失敗]
# }
@contextmanager
def _open_planning_reader(path: Path) -> Iterator[BinaryIO]:
    if os.name == "nt":
        with _open_windows_reader(path) as stream:
            yield stream
    else:
        with path.open("rb") as stream:
            yield stream


# {
#   責務: [_open_windows_reader: writerのatomic replaceと競合しないWindows読取を行う]
#   処理: [CreateFileWでREAD/WRITE/DELETE共有、OPEN_EXISTING、GENERIC_READだけを使う。
#     CRT fd化成功まではHANDLEを所有し、成功後はfdを一度だけcloseする。
#     streamはclosefd=Falseでfdを借り、初期化/読取例外でも所有権を二重移譲しない]
#   引数: [path: planner所有の既存file]
#   戻り値: [BinaryIOのcontext。handleはcontext外へ持ち出さない]
#   エラー: [OSError: Windows API/CRT/open/read/close失敗]
# }
@contextmanager
def _open_windows_reader(path: Path) -> Iterator[BinaryIO]:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = _windows_file_api()
    win_error: Callable[[int], OSError] = getattr(ctypes, "WinError")
    last_error: Callable[[], int] = getattr(ctypes, "get_last_error")
    open_descriptor: Callable[[int, int], int] = getattr(msvcrt, "open_osfhandle")
    binary_flag: int = getattr(os, "O_BINARY")
    create_file = kernel.CreateFileW
    close_handle = kernel.CloseHandle
    generic_read = 0x80000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    handle: int | None = create_file(
        str(path),
        generic_read,
        share_read_write_delete,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise win_error(last_error())
    try:
        descriptor = open_descriptor(handle, os.O_RDONLY | binary_flag)
    except BaseException:
        if not close_handle(handle):
            raise win_error(last_error())
        raise
    try:
        # fd remains owned here even if stream construction raises partway through.
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


# {
#   責務: [_windows_file_api: kernel32のABIを初回だけ解決し、pollごとの再ロードを避ける]
#   処理: [同じ読み取り専用ABI定義をprocess内で共有する。file HANDLEはcacheしない]
#   引数: [なし] 戻り値: [Any: ctypesが返すplatform固有のDLL binding]
#   エラー: [OSError/AttributeError: Windows APIを解決できない]
# }
@lru_cache(maxsize=1)
def _windows_file_api() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return kernel
