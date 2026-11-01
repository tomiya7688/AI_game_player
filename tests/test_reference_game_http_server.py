from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, RemoteDisconnected
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
import tempfile
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlsplit

from tools.reference_browser_diagnostics import RESOURCE_NAMES, ResourceResponseCounts
from tools.reference_game_2048_launcher import LoopbackStaticServer
from tools.reference_game_http_server import ReferenceGameHTTPServer


class ReferenceGameHttpServerTests(unittest.TestCase):
    def test_finite_queue_is_configured_before_listen_and_keeps_private_address(self):
        stream = Mock()
        stream.getsockname.return_value = ("127.0.0.1", 43210)
        with patch("socketserver.socket.socket", return_value=stream):
            server = ReferenceGameHTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
            try:
                stream.bind.assert_called_once_with(("127.0.0.1", 0))
                stream.listen.assert_called_once_with(32)
                calls = [call[0] for call in stream.method_calls]
                self.assertLess(calls.index("bind"), calls.index("listen"))
                self.assertEqual(server.server_address, ("127.0.0.1", 43210))
                self.assertTrue(server.daemon_threads)
                self.assertEqual(SimpleHTTPRequestHandler.protocol_version, "HTTP/1.0")
            finally:
                server.server_close()
        stream.close.assert_called_once()

    def test_concurrent_ten_scripts_preserve_bytes_counters_and_port_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "index.html").write_bytes(b"<head></head>")
            scripts = [name for name in RESOURCE_NAMES if name.startswith("/js/")]
            expected = {}
            for name in scripts:
                path = source / name.lstrip("/")
                path.parent.mkdir(exist_ok=True)
                expected[name] = (name + "\n// fixed fixture 試験\n").encode("utf-8")
                path.write_bytes(expected[name])
            server = LoopbackStaticServer()
            server.post_failure_render_diagnostics = True
            url = server.start(source)
            original_thread = server._thread
            barrier = threading.Barrier(len(scripts))

            def read(name):
                barrier.wait(2)
                with urlopen(url + name.lstrip("/"), timeout=2) as response:
                    return name, response.read(65537), response.status, response.version

            try:
                with ThreadPoolExecutor(max_workers=len(scripts)) as executor:
                    values = list(executor.map(read, scripts))
                self.assertEqual(len(values), 10)
                for name, body, status, version in values:
                    self.assertEqual(body, expected[name])
                    self.assertEqual((status, version), (200, 10))
                self.assertFalse(
                    server.resource_response_summary()["recording_incomplete"]
                )
                resources = server.resource_response_summary()["resources"]
                self.assertEqual(
                    set(resources), {RESOURCE_NAMES[path] for path in scripts}
                )
                for name in scripts:
                    value = resources[RESOURCE_NAMES[name]]
                    self.assertEqual(value["requests_started"], 1)
                    self.assertEqual(value["headers_completed"], 1)
                    self.assertEqual(value["responses"], 1)
                    self.assertEqual(value["completed"], 1)
                    self.assertEqual(value["bytes_written"], len(expected[name]))
                    self.assertEqual(value["failed_requests"], 0)
                    self.assertIsNone(value["last_failed_phase"])
                self.assertTrue(original_thread.is_alive())
            finally:
                server.close()
            self.assertFalse(original_thread.is_alive())
            with self.assertRaises(URLError):
                urlopen(url, timeout=1)
            server.close()

    def test_static_200_header_failure_creates_started_row_without_false_response(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "index.html").write_bytes(b"fixture")
            (source / "js").mkdir()
            (source / "js/html_actuator.js").write_bytes(b"window.HTMLActuator = 1;")
            server = LoopbackStaticServer()
            server.post_failure_render_diagnostics = True
            url = server.start(source)
            try:
                with (
                    patch.object(
                        SimpleHTTPRequestHandler,
                        "end_headers",
                        side_effect=BrokenPipeError,
                    ),
                    patch.object(server._server, "handle_error"),
                    self.assertRaises((RemoteDisconnected, URLError)),
                ):
                    urlopen(url + "js/html_actuator.js", timeout=2)
                row = server.resource_response_summary()["resources"][
                    "html_actuator_js"
                ]
                self.assertEqual(row["requests_started"], 1)
                self.assertEqual(row["headers_completed"], 0)
                self.assertEqual(row["responses"], 0)
                self.assertEqual(row["completed"], 0)
                self.assertEqual(row["bytes_written"], 0)
                self.assertEqual(row["failed_requests"], 1)
                self.assertEqual(row["last_failed_phase"], "headers")
            finally:
                server.close()

    def test_head_and_unsupported_methods_keep_http_semantics_without_get_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "index.html").write_bytes(b"fixture")
            server = LoopbackStaticServer()
            server.post_failure_render_diagnostics = True
            url = server.start(source)
            try:
                for method, expected in (("HEAD", 200), ("BREW", 501)):
                    connection = HTTPConnection(
                        "127.0.0.1", urlsplit(url).port, timeout=2
                    )
                    try:
                        connection.request(method, "/")
                        response = connection.getresponse()
                        self.assertEqual(response.status, expected)
                        body = response.read(65537)
                        if method == "HEAD":
                            self.assertEqual(body, b"")
                    finally:
                        connection.close()
                self.assertEqual(server.resource_response_summary()["resources"], {})
            finally:
                server.close()

    def test_response_counter_oom_preserves_original_header_send_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "index.html").write_bytes(b"fixture")
            server = LoopbackStaticServer()
            server.test_seed = 481
            # The seeded branch records failures from inside its finally block.
            with patch(
                "tools.reference_game_2048_launcher.seeded_index",
                return_value=(b"fixture", {}),
            ):
                server.post_failure_render_diagnostics = True
                url = server.start(source)
            primary = BrokenPipeError(32, "fixed failure")
            observed = []
            try:
                with (
                    patch.object(
                        SimpleHTTPRequestHandler, "end_headers", side_effect=primary
                    ),
                    patch.object(
                        ResourceResponseCounts, "_entry", side_effect=MemoryError
                    ),
                    patch.object(
                        server._server,
                        "handle_error",
                        side_effect=lambda *_: observed.append(sys.exc_info()[1]),
                    ),
                    self.assertRaises((RemoteDisconnected, URLError)),
                ):
                    urlopen(url, timeout=2)
                self.assertEqual(len(observed), 1)
                self.assertIs(observed[0], primary)
                self.assertTrue(
                    server.resource_response_summary()["recording_incomplete"]
                )
            finally:
                server.close()

    def test_phase_counters_are_fixed_allowlist_bounded_and_failure_noninterfering(
        self,
    ):
        counts = ResourceResponseCounts()
        self.assertFalse(counts.snapshot()["recording_incomplete"])
        counts.started("grid_js")
        counts._resources["grid_js"]["requests_started"] = 2**31 - 1
        counts.started("grid_js")
        counts.failed("grid_js", "body")
        counts.failed("grid_js", "private text")
        counts.started("/private?token=value")
        row = counts.snapshot()["resources"]["grid_js"]
        self.assertEqual(row["requests_started"], 2**31 - 1)
        self.assertEqual(row["failed_requests"], 1)
        self.assertEqual(row["last_failed_phase"], "body")
        self.assertEqual(list(counts.snapshot()["resources"]), ["grid_js"])
        with patch.object(counts, "_entry", side_effect=MemoryError):
            counts.started("tile_js")
            counts.headers_completed("tile_js")
            counts.failed("tile_js", "headers")
        self.assertEqual(list(counts.snapshot()["resources"]), ["grid_js"])
        self.assertTrue(counts.snapshot()["recording_incomplete"])
        record_only = ResourceResponseCounts()
        record_only.record("grid_js", 200, 44, "a" * 64, True)
        original_resources = record_only.snapshot()["resources"]
        self.assertFalse(record_only.snapshot()["recording_incomplete"])
        with patch.object(record_only, "_entry", side_effect=MemoryError):
            record_only.record("tile_js", 200, 10, "b" * 64, True)
        self.assertTrue(record_only.snapshot()["recording_incomplete"])
        self.assertEqual(record_only.snapshot()["resources"], original_resources)
