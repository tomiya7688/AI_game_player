import ctypes
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_game_player.planning_file_publisher import (
    _RenameInfoEx,
    _rename_buffer,
    _replace_windows_planning_file,
    _windows_publish_api,
    replace_planning_file,
)
from ai_game_player.planning_wire import publish_planning_json, read_planning_json


class PlanningFilePublisherTests(unittest.TestCase):
    def tearDown(self):
        _windows_publish_api.cache_clear()

    def test_native_layout_and_utf16_byte_length(self):
        target = Path("checkpoint-試験-😀.json")
        buffer = _rename_buffer(target)
        header = _RenameInfoEx.from_buffer(buffer)
        expected = os.path.abspath(target).encode("utf-16-le")
        self.assertEqual(header.Flags, 3)
        self.assertIsNone(header.RootDirectory)
        self.assertEqual(header.FileNameLength, len(expected))
        self.assertEqual(
            _RenameInfoEx.RootDirectory.offset, ctypes.sizeof(ctypes.c_void_p)
        )
        self.assertEqual(
            _RenameInfoEx.FileName.offset, 2 * ctypes.sizeof(ctypes.c_void_p) + 4
        )
        self.assertEqual(
            buffer.raw[_RenameInfoEx.FileName.offset :][: len(expected)], expected
        )
        self.assertEqual(
            buffer.raw[_RenameInfoEx.FileName.offset + len(expected) :][:2], b"\0\0"
        )
        with self.assertRaises(ValueError):
            _rename_buffer(Path("bad\0path"))
        with (
            patch("os.path.abspath", return_value="a" * 32768),
            self.assertRaises(ValueError),
        ):
            _rename_buffer(Path("long-target"))

    def kernel(self, *, handle=123, rename=1):
        return SimpleNamespace(
            CreateFileW=Mock(return_value=handle),
            SetFileInformationByHandle=Mock(return_value=rename),
            CloseHandle=Mock(return_value=1),
        )

    def native_context(self, kernel):
        return (
            patch("ctypes.WinDLL", return_value=kernel, create=True),
            patch(
                "ctypes.WinError",
                side_effect=lambda value: OSError(value, "native failure"),
                create=True,
            ),
            patch("ctypes.get_last_error", return_value=50, create=True),
        )

    def test_native_call_has_delete_access_info_class_and_exact_ownership(self):
        kernel = self.kernel()
        api, error, last = self.native_context(kernel)
        with api, error, last:
            _replace_windows_planning_file(Path("source"), Path("target"))
        kernel.CreateFileW.assert_called_once_with(
            "source", 0x10000, 7, None, 3, 0x80, None
        )
        args = kernel.SetFileInformationByHandle.call_args.args
        self.assertEqual(args[:2], (123, 22))
        self.assertEqual(_RenameInfoEx.from_buffer(args[2]).Flags, 3)
        self.assertEqual(args[3], ctypes.sizeof(args[2]))
        kernel.CloseHandle.assert_called_once_with(123)
        self.assertEqual(len(kernel.SetFileInformationByHandle.argtypes), 4)

    def test_unsupported_rename_fails_closed_without_replace_retry_or_delete(self):
        kernel = self.kernel(rename=0)
        api, error, last = self.native_context(kernel)
        with (
            api,
            error,
            last,
            patch("os.replace") as fallback,
            self.assertRaises(OSError),
        ):
            _replace_windows_planning_file(Path("source"), Path("target"))
        self.assertEqual(kernel.SetFileInformationByHandle.call_count, 1)
        kernel.CloseHandle.assert_called_once_with(123)
        fallback.assert_not_called()

    def test_invalid_source_handle_is_not_closed(self):
        kernel = self.kernel(handle=ctypes.c_void_p(-1).value)
        api, error, last = self.native_context(kernel)
        with api, error, last, self.assertRaises(OSError):
            _replace_windows_planning_file(Path("source"), Path("target"))
        kernel.CloseHandle.assert_not_called()
        kernel.SetFileInformationByHandle.assert_not_called()

    def test_missing_api_is_a_failclosed_os_error(self):
        with (
            patch("ctypes.WinDLL", return_value=SimpleNamespace(), create=True),
            self.assertRaises(OSError),
        ):
            _windows_publish_api()

    def test_rename_error_remains_primary_when_close_reports_failure(self):
        kernel = self.kernel(rename=0)
        kernel.CloseHandle.return_value = 0
        api, error, last = self.native_context(kernel)
        with api, error, last as last_code, self.assertRaises(OSError) as caught:
            last_code.side_effect = [50, 6]
            _replace_windows_planning_file(Path("source"), Path("target"))
        self.assertEqual(caught.exception.errno, 50)
        self.assertEqual(last_code.call_count, 1)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_non_windows_preserves_os_replace(self):
        source, target = Path("source"), Path("target")
        with (
            patch("ai_game_player.planning_file_publisher.os.name", "posix"),
            patch("os.replace") as native,
        ):
            replace_planning_file(source, target)
        native.assert_called_once_with(source, target)

    def test_publication_failure_keeps_old_message_and_discards_only_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.json"
            publish_planning_json(path, {"version": 1}, 100)
            with (
                patch(
                    "ai_game_player.planning_wire.replace_planning_file",
                    side_effect=OSError("unsupported"),
                ),
                self.assertRaises(OSError),
            ):
                publish_planning_json(path, {"version": 2}, 100)
            self.assertEqual(read_planning_json(path, 100), {"version": 1})
            self.assertEqual(list(path.parent.iterdir()), [path])
