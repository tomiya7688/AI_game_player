"""一時fileだけでWindows renameのopen-target semanticsを有界に調べる開発probe。"""

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from ai_game_player.planning_file_publisher import (
    _RenameInfoEx,
    _replace_windows_planning_file,
    _windows_publish_api,
)
from ai_game_player.planning_file_reader import _open_planning_reader


@contextmanager
def _reader(path: Path, mode: str, kernel: Any) -> Iterator[Callable[[], bytes] | None]:
    """HANDLE→CRT→streamの各段階を別fixtureで保持し、所有権を一度だけ解放。"""
    if mode == "none":
        yield None
        return
    if mode in ("ordinary", "production"):
        context = path.open("rb") if mode == "ordinary" else _open_planning_reader(path)
        with context as stream:
            yield lambda: stream.read(101)
        return
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
    if mode == "native":
        try:

            def read_native() -> bytes:
                buffer = ctypes.create_string_buffer(101)
                count = ctypes.c_uint32()
                if not kernel.ReadFile(handle, buffer, 101, ctypes.byref(count), None):
                    raise getattr(ctypes, "WinError")(
                        getattr(ctypes, "get_last_error")()
                    )
                return buffer.raw[: count.value]

            yield read_native
        finally:
            if not kernel.CloseHandle(handle):
                raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        return
    import msvcrt

    try:
        descriptor = getattr(msvcrt, "open_osfhandle")(
            handle, os.O_RDONLY | getattr(os, "O_BINARY")
        )
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        if getattr(msvcrt, "get_osfhandle")(descriptor) != handle:
            raise RuntimeError("native descriptor identity changed")
        if mode == "crt":
            yield lambda: os.read(descriptor, 101)
        else:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                yield lambda: stream.read(101)
    finally:
        os.close(descriptor)


def _operation(kernel: Any, mode: str, source: Path, target: Path) -> None:
    """各fixtureへ一度だけ操作し、失敗をretryしない。"""
    if mode == "rename_source":
        os.rename(target, target.with_name("moved.json"))
    elif mode == "os_replace":
        os.replace(source, target)
    elif mode == "posix_rename":
        _replace_windows_planning_file(source, target)
    else:
        succeeded = (
            kernel.MoveFileExW(str(source), str(target), 1)
            if mode == "movefileex"
            else kernel.ReplaceFileW(str(target), str(source), None, 0, None, None)
        )
        if not succeeded:
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())


def run_probe() -> dict[str, Any]:
    """全30ケースを固定byte内容で実行。任意path/exception本文/handle値は出力しない。"""
    if os.name != "nt":
        return {
            "platform": "non_windows",
            "native_semantics": "unverified",
            "cases": [],
        }
    from ctypes import wintypes

    kernel = _windows_publish_api()
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_uint32),
        wintypes.LPVOID,
    ]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel.MoveFileExW.restype = wintypes.BOOL
    kernel.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    kernel.ReplaceFileW.restype = wintypes.BOOL
    cases = []
    for reader in ("none", "ordinary", "native", "crt", "stream", "production"):
        for writer in (
            "rename_source",
            "os_replace",
            "movefileex",
            "replacefile",
            "posix_rename",
        ):
            result: dict[str, Any] = {
                "reader": reader,
                "writer": writer,
                "phase": "setup",
            }
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "target.json"
                source = Path(directory) / "source.json"
                target.write_bytes(b'{"version":1}')
                source.write_bytes(b'{"version":2}')
                try:
                    with _reader(target, reader, kernel) as held:
                        result["phase"] = "operation"
                        try:
                            _operation(kernel, writer, source, target)
                            result["success"] = True
                        except OSError as error:
                            result.update(
                                success=False,
                                errno=error.errno,
                                winerror=getattr(error, "winerror", None),
                            )
                        result["fresh_open_while_held"] = (
                            "absent"
                            if not target.exists()
                            else "new"
                            if target.read_bytes() == b'{"version":2}'
                            else "old"
                        )
                        result["phase"] = "held_read"
                        result["held_old_snapshot"] = (
                            None if held is None else held() == b'{"version":1}'
                        )
                    result["target_state"] = (
                        "absent"
                        if not target.exists()
                        else "new"
                        if target.read_bytes() == b'{"version":2}'
                        else "old"
                    )
                    result["phase"] = "complete"
                except (OSError, RuntimeError) as error:
                    result.update(
                        infrastructure_error=True,
                        errno=getattr(error, "errno", None),
                        winerror=getattr(error, "winerror", None),
                    )
            cases.append(result)
            print(json.dumps(result, sort_keys=True), flush=True)
    return {
        "platform": "windows",
        "info_class": 22,
        "rename_flags": 3,
        "reader_share": 7,
        "pointer_bytes": ctypes.sizeof(ctypes.c_void_p),
        "name_offset": _RenameInfoEx.FileName.offset,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_probe()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return int(any(case.get("infrastructure_error") for case in result["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
