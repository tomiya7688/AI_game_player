"""Post-failure diagnostics cannot become an observation or a recovery action."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tools import reference_browser_diagnostics as diag
from tools.reference_game_2048_launcher import (
    BrowserWindowSession,
    LoopbackStaticServer,
)


def render_value():
    return {
        **dict.fromkeys(diag.BOOLEAN_FIELDS, True),
        "ready_state": "complete",
        "visibility": "visible",
        "tile_count": 2,
        "resource_timing": {
            "state": "unavailable",
            "script_count_capped": 0,
            "script_overflow": False,
            "resource_count_capped": None,
            "resources": {
                name: dict.fromkeys(diag.TIMING_FIELDS)
                for name in diag.TIMING_RESOURCE_NAMES
            },
        },
    }


class DiagnosticBoundaryTests(unittest.TestCase):
    def test_fixed_expression_has_no_values_storage_input_or_callbacks(self):
        for forbidden in (
            "localStorage.",
            "textContent",
            "innerHTML",
            ".score",
            "requestAnimationFrame",
            ".move(",
            "reload(",
            "console.",
            "Array.from(",
        ):
            self.assertNotIn(forbidden, diag.RENDER_EXPRESSION)
        self.assertEqual(16, len(render_value()))

    def test_owner_url_target_field_and_byte_rejection(self):
        for url in (
            "https://127.0.0.1:123/",
            "http://localhost:123/",
            "http://127.0.0.1:123/?x=token",
            "http://u@127.0.0.1:123/",
            "http://127.0.0.1:123/other",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                diag._expected_url(url)
        url = "http://127.0.0.1:123/"
        target = {
            "type": "page",
            "url": url,
            "webSocketDebuggerUrl": "ws://127.0.0.1:456/devtools/page/abc",
        }
        self.assertEqual(
            target["webSocketDebuggerUrl"], diag._target_socket([target], url, 456)
        )
        for targets in (
            [target, target],
            [{**target, "url": url + "other"}],
            [
                {
                    **target,
                    "webSocketDebuggerUrl": "ws://example.com:456/devtools/page/abc",
                }
            ],
        ):
            with self.assertRaises(ValueError):
                diag._target_socket(targets, url, 456)
        for value in (
            {**render_value(), "secret": "free text"},
            {**render_value(), "tile_count": 33},
            {**render_value(), "tile_count": True},
            {**render_value(), "ready_state": "console text"},
        ):
            with self.assertRaises(ValueError):
                diag._sanitize(value)
        with self.assertRaises(ValueError):
            diag._decode(json.dumps({str(n): None for n in range(257)}))
        with self.assertRaises(ValueError):
            diag._decode("[" * 10 + "0" + "]" * 10)

    def test_private_port_file_is_bounded_and_tokens_never_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            file = profile / "DevToolsActivePort"
            file.write_text("456\n/devtools/browser/private-token\n")
            self.assertEqual(456, diag._port(profile))
            for content in (
                "0\n/devtools/browser/private-token",
                "456\nws://other/secret",
                "1" * 1025,
            ):
                file.write_text(content)
                with self.assertRaises(ValueError):
                    diag._port(profile)

    def test_port_rejects_symlink_fifo_and_reparse_before_open(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            path = profile / "DevToolsActivePort"
            target = profile / "regular"
            target.write_text("456\n/devtools/browser/private-token\n")
            if hasattr(os, "symlink"):
                try:
                    path.symlink_to(target)
                except OSError:
                    pass  # Windows may not grant symbolic-link creation rights.
                else:
                    with (
                        self.assertRaises(ValueError),
                        patch.object(diag.os, "open") as opened,
                    ):
                        diag._port(profile)
                    opened.assert_not_called()
                    path.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(path)
                started = time.monotonic()
                with (
                    self.assertRaises(ValueError),
                    patch.object(diag.os, "open") as opened,
                ):
                    diag._port(profile)
                opened.assert_not_called()
                self.assertLess(time.monotonic() - started, 0.1)
                path.unlink()
            path.write_text(target.read_text())
            original_profile = profile.lstat()
            original_file = path.lstat()
            reparse_file = SimpleNamespace(
                st_mode=original_file.st_mode,
                st_size=original_file.st_size,
                st_file_attributes=0x400,
            )
            with (
                patch.object(
                    Path, "lstat", side_effect=[original_profile, reparse_file]
                ),
                patch.object(diag.os, "open") as opened,
            ):
                with self.assertRaises(ValueError):
                    diag._port(profile)
                opened.assert_not_called()

    def test_seeded_broken_pipe_is_counted_without_claiming_bytes_or_success(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source / "index.html").touch()
            server = LoopbackStaticServer()
            server.post_failure_render_diagnostics = True
            fake_http = Mock(server_address=("127.0.0.1", 123))
            with (
                patch(
                    "tools.reference_game_2048_launcher.seeded_index",
                    return_value=(b"seeded page", {}),
                ),
                patch(
                    "tools.reference_game_2048_launcher.ReferenceGameHTTPServer",
                    return_value=fake_http,
                ) as http,
                patch("tools.reference_game_2048_launcher.threading.Thread"),
            ):
                server.start(source)
                handler_type = http.call_args.args[1].func
                for failure_phase in ("headers", "body"):
                    handler = object.__new__(handler_type)
                    handler.path = "/"
                    handler.send_response = Mock()
                    handler.send_header = Mock()
                    handler.end_headers = Mock(
                        side_effect=BrokenPipeError()
                        if failure_phase == "headers"
                        else None
                    )
                    handler.wfile = Mock()
                    handler.wfile.write.side_effect = BrokenPipeError()
                    with self.assertRaises(BrokenPipeError):
                        handler.do_GET()
                summary = server.resource_response_summary()["resources"]["page"]
                self.assertEqual(2, summary["responses"])
                self.assertEqual(0, summary["completed"])
                self.assertEqual(0, summary["bytes_written"])
                self.assertEqual(2, summary["unknown_write_counts"])
                self.assertFalse(summary["last_write_count_known"])
                self.assertIsNone(summary["last_body_sha256"])

    def test_one_shot_and_failure_image_barrier(self):
        session = BrowserWindowSession(Path("browser"), Path("source"))
        with patch.object(
            diag, "inspect_failed_browser", return_value={"status": "observed"}
        ) as inspect:
            self.assertEqual(
                "not_enabled",
                session.collect_failure_render_diagnostics(
                    failure_confirmed=True, frame_preserved=True
                )["reason"],
            )
            session.post_failure_render_diagnostics = True
            self.assertEqual(
                "failure_frame_not_preserved",
                session.collect_failure_render_diagnostics(
                    failure_confirmed=True, frame_preserved=False
                )["reason"],
            )
            inspect.assert_not_called()
            session._profile = SimpleNamespace(name="private")
            session._diagnostic_url = "http://127.0.0.1:123/"
            session.process = Mock(pid=100)
            session.window_info = SimpleNamespace(handle=200)
            self.assertEqual(
                "observed",
                session.collect_failure_render_diagnostics(
                    failure_confirmed=True, frame_preserved=True
                )["status"],
            )
            self.assertEqual(
                "already_attempted",
                session.collect_failure_render_diagnostics(
                    failure_confirmed=True, frame_preserved=True
                )["reason"],
            )
            inspect.assert_called_once()

    def test_default_flags_unchanged_and_opt_in_is_loopback_ephemeral(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "browser.exe"
            executable.touch()
            for enabled in (False, True):
                session = BrowserWindowSession(
                    executable, Path(folder), post_failure_render_diagnostics=enabled
                )
                process = Mock(pid=100)
                with (
                    patch("tools.reference_game_2048_launcher.os.name", "nt"),
                    patch(
                        "tools.reference_game_2048_launcher.subprocess.Popen",
                        return_value=process,
                    ) as spawn,
                    patch.object(
                        session,
                        "_wait_for_window",
                        return_value=SimpleNamespace(handle=123),
                    ),
                    patch(
                        "tools.reference_game_2048_launcher.focus_resize_browser_window",
                        return_value=True,
                    ),
                ):
                    session.start("http://127.0.0.1:123/")
                arguments = spawn.call_args.args[0]
                debugging = [
                    arg for arg in arguments if arg.startswith("--remote-debugging")
                ]
                self.assertEqual(
                    [
                        "--remote-debugging-address=127.0.0.1",
                        "--remote-debugging-port=0",
                    ]
                    if enabled
                    else [],
                    debugging,
                )
                session._profile.cleanup()

    def test_deadline_and_fixed_error_no_endpoint_or_console(self):
        async def slow(*args):
            await asyncio.sleep(2)

        started = time.monotonic()
        with patch.object(diag, "_inspect", side_effect=slow):
            result = diag.inspect_failed_browser(
                Path("profile"), "private-url", Mock(), 2
            )
        self.assertEqual("deadline", result["reason"])
        self.assertLess(time.monotonic() - started, 1.15)
        with patch.object(
            diag, "_inspect", side_effect=RuntimeError("ws://secret Console: sensitive")
        ):
            result = diag.inspect_failed_browser(
                Path("profile"), "private-url", Mock(), 2
            )
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("sensitive", json.dumps(result))

    def test_allowlist_response_summary_is_not_browser_receipt(self):
        counts = diag.ResourceResponseCounts()
        counts.record("application_js", 200, 44, "a" * 64, True)
        counts.record("not-allowed?token=secret", 200, 1, "b" * 64, True)
        snapshot = counts.snapshot()
        self.assertTrue(snapshot["server_write_only"])
        self.assertEqual(["application_js"], list(snapshot["resources"]))
        self.assertEqual(44, snapshot["resources"]["application_js"]["bytes_written"])
        self.assertEqual(
            {"status": "not_enabled"},
            LoopbackStaticServer().resource_response_summary(),
        )


class DiagnosticTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_websocket_library_fixed_request_and_event_limit(self):
        try:
            from websockets.asyncio.server import serve
        except ImportError:
            self.skipTest("optional websockets validation dependency")
        requests = []
        mode = "valid"

        async def handler(socket):
            request = json.loads(await socket.recv())
            requests.append(request)
            if mode == "events":
                for _ in range(diag.MAX_EVENTS + 1):
                    await socket.send(
                        json.dumps(
                            {
                                "method": "console.secret",
                                "params": {"text": "sensitive"},
                            }
                        )
                    )
            elif mode == "oversize":
                await socket.send("x" * (diag.MAX_MESSAGE_BYTES + 1))
            else:
                await socket.send(
                    json.dumps(
                        {
                            "id": 1,
                            "result": {
                                "result": {"type": "object", "value": render_value()}
                            },
                        }
                    )
                )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            target = {
                "type": "page",
                "url": "http://127.0.0.1:123/",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/abc",
            }
            with tempfile.TemporaryDirectory() as folder:
                profile = Path(folder)
                (profile / "DevToolsActivePort").write_text(
                    f"{port}\n/devtools/browser/private-token\n"
                )
                with (
                    patch.object(
                        diag, "_targets", new=AsyncMock(return_value=[target])
                    ),
                    patch.object(diag, "_owned_window", return_value=True),
                ):
                    value = await diag._inspect(profile, target["url"], Mock(), 2)
                    self.assertEqual(render_value(), value)
                    self.assertEqual(
                        [
                            {
                                "id": 1,
                                "method": "Runtime.evaluate",
                                "params": {
                                    "expression": diag.RENDER_EXPRESSION,
                                    "returnByValue": True,
                                    "silent": True,
                                    "awaitPromise": False,
                                    "disableBreaks": True,
                                    "timeout": 500,
                                },
                            }
                        ],
                        requests,
                    )
                    mode = "events"
                    with self.assertRaisesRegex(ValueError, "event_limit"):
                        await diag._inspect(profile, target["url"], Mock(), 2)
                    mode = "oversize"
                    with self.assertRaises(Exception):
                        await diag._inspect(profile, target["url"], Mock(), 2)

    async def test_http_content_length_bound_and_no_redirect(self):
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]"

        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(response)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            self.assertEqual([], await diag._targets(port))
            for content in (
                b"HTTP/1.1 302 Found\r\nLocation: http://other/\r\nContent-Length: 2\r\n\r\n[]",
                b"HTTP/1.1 200 OK\r\nContent-Length: 16385\r\n\r\n",
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n[]",
            ):
                response = content
                with self.assertRaises(ValueError):
                    await diag._targets(port)


if __name__ == "__main__":
    unittest.main()
