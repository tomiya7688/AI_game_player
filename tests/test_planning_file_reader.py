from contextlib import ExitStack
import ctypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_game_player.planning_file_reader import (
    _open_planning_reader,
    _open_windows_reader,
    _windows_file_api,
    read_planning_bytes,
)
from ai_game_player.planning_wire import publish_planning_json, read_planning_json


class PlanningFileReaderTests(unittest.TestCase):
    def test_byte_limit_and_json_guards_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.json"
            path.write_bytes(b'{"x":1}')
            self.assertEqual(read_planning_bytes(path, 7), b'{"x":1}')
            self.assertEqual(read_planning_json(path, 7), {"x": 1})
            with self.assertRaises(ValueError):
                read_planning_bytes(path, 6)
            for maximum in (-1, True, 1.5):
                with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                    read_planning_bytes(path, maximum)
            for payload in (b'{"x":1,"x":2}', b'{"x":NaN}', b"[]"):
                path.write_bytes(payload)
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    read_planning_json(path, 100)
            path.write_bytes(b"[" * 33 + b"0" + b"]" * 33)
            with self.assertRaises(ValueError):
                read_planning_json(path, 100)

    def test_open_reader_keeps_old_snapshot_while_replacement_becomes_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            publish_planning_json(path, {"version": 1}, 100)
            with _open_planning_reader(path) as opened:
                publish_planning_json(path, {"version": 2}, 100)
                self.assertEqual(json.loads(opened.read(101)), {"version": 1})
            self.assertEqual(read_planning_json(path, 100), {"version": 2})

    @unittest.skipUnless(os.name == "nt", "requires actual Windows CreateFileW/CRT")
    def test_windows_atomic_replace_with_reader_held_open_20_times(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            publish_planning_json(path, {"version": 0}, 100)
            for version in range(20):
                with self.subTest(version=version):
                    # The actual native handle stays open across os.replace in publish.
                    with _open_planning_reader(path) as opened:
                        publish_planning_json(path, {"version": version + 1}, 100)
                        self.assertEqual(
                            json.loads(opened.read(101)), {"version": version}
                        )
                    self.assertEqual(
                        read_planning_json(path, 100), {"version": version + 1}
                    )

    def windows_mocks(self, *, handle=123, fd=17):
        stack = ExitStack()
        _windows_file_api.cache_clear()
        stack.callback(_windows_file_api.cache_clear)
        kernel = SimpleNamespace(
            CreateFileW=Mock(return_value=handle), CloseHandle=Mock(return_value=1)
        )
        crt = SimpleNamespace(open_osfhandle=Mock(return_value=fd))
        stack.enter_context(patch("ctypes.WinDLL", return_value=kernel, create=True))
        stack.enter_context(
            patch(
                "ctypes.WinError",
                side_effect=lambda code: OSError(code, "native error"),
                create=True,
            )
        )
        stack.enter_context(patch("ctypes.get_last_error", return_value=5, create=True))
        stack.enter_context(patch.dict(sys.modules, {"msvcrt": crt}))
        stack.enter_context(patch.object(os, "O_BINARY", 0x8000, create=True))
        return stack, kernel, crt

    def test_windows_abi_flags_and_exact_descriptor_ownership(self):
        stack, kernel, crt = self.windows_mocks()
        borrowed = io.BytesIO(b"message")
        with (
            stack,
            patch("os.fdopen", return_value=borrowed) as fdopen,
            patch("os.close") as close_fd,
        ):
            with _open_windows_reader(Path("checkpoint.json")) as stream:
                self.assertEqual(stream.read(8), b"message")
                close_fd.assert_not_called()
            self.assertTrue(borrowed.closed)
            kernel.CloseHandle.assert_not_called()
            crt.open_osfhandle.assert_called_once_with(123, os.O_RDONLY | 0x8000)
            fdopen.assert_called_once_with(17, "rb", closefd=False)
            close_fd.assert_called_once_with(17)
            args = kernel.CreateFileW.call_args.args
            self.assertEqual(args[:3], ("checkpoint.json", 0x80000000, 7))
            self.assertEqual(args[3:], (None, 3, 0x80, None))
            self.assertEqual(len(kernel.CreateFileW.argtypes), 7)

    def test_invalid_native_handle_is_not_closed_or_converted(self):
        stack, kernel, crt = self.windows_mocks(handle=ctypes.c_void_p(-1).value)
        with stack, self.assertRaises(OSError):
            with _open_windows_reader(Path("missing.json")):
                self.fail("invalid handle must not yield")
        kernel.CloseHandle.assert_not_called()
        crt.open_osfhandle.assert_not_called()

    def test_crt_conversion_failure_closes_only_native_handle(self):
        stack, kernel, crt = self.windows_mocks()
        crt.open_osfhandle.side_effect = OSError("conversion failed")
        with stack, patch("os.close") as close_fd, self.assertRaises(OSError):
            with _open_windows_reader(Path("checkpoint.json")):
                self.fail("failed conversion must not yield")
        kernel.CloseHandle.assert_called_once_with(123)
        close_fd.assert_not_called()

    def test_stream_construction_failure_closes_only_descriptor(self):
        stack, kernel, _crt = self.windows_mocks()
        with (
            stack,
            patch("os.fdopen", side_effect=OSError("stream failed")),
            patch("os.close") as close_fd,
            self.assertRaises(OSError),
        ):
            with _open_windows_reader(Path("checkpoint.json")):
                self.fail("failed stream must not yield")
        kernel.CloseHandle.assert_not_called()
        close_fd.assert_called_once_with(17)

    def test_read_exception_still_closes_exactly_one_descriptor(self):
        stack, kernel, _crt = self.windows_mocks()
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.read.side_effect = OSError("read failed")
        with (
            stack,
            patch("os.fdopen", return_value=stream),
            patch("os.close") as close_fd,
            self.assertRaises(OSError),
        ):
            with _open_windows_reader(Path("checkpoint.json")) as opened:
                opened.read(9)
        kernel.CloseHandle.assert_not_called()
        close_fd.assert_called_once_with(17)
