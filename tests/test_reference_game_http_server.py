from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, RemoteDisconnected
from http.server import SimpleHTTPRequestHandler
import io
from pathlib import Path
import socket
import tempfile
import sys
import threading
import time
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
                # Generous bounds: 11 threads on a loaded Windows host can stall for seconds.
                barrier.wait(15)
                with urlopen(url + name.lstrip("/"), timeout=15) as response:
                    return name, response.read(65537), response.status, response.version

            try:
                with ThreadPoolExecutor(max_workers=len(scripts)) as executor:
                    values = list(executor.map(read, scripts))
                self.assertEqual(len(values), 10)
                for name, body, status, version in values:
                    self.assertEqual(body, expected[name])
                    self.assertEqual((status, version), (200, 10))
                # The server updates its counters after the response is written, which can be
                # after the client has already received the body; wait (bounded) for them.
                deadline = time.monotonic() + 10
                while True:
                    summary = server.resource_response_summary()
                    resources = summary["resources"]
                    if all(
                        resources[RESOURCE_NAMES[path]]["completed"] == 1
                        for path in scripts
                    ):
                        break
                    self.assertLess(
                        time.monotonic(),
                        deadline,
                        f"server counters did not settle: {resources}",
                    )
                    time.sleep(0.02)
                self.assertFalse(summary["recording_incomplete"])
                # 診断有効時は固定12名が0で初期化され、未要求のpage/main_cssも行を持つ。
                self.assertEqual(set(resources), set(RESOURCE_NAMES.values()))
                for name in ("page", "main_css"):
                    self.assertEqual(resources[name]["requests_started"], 0)
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
                resources = server.resource_response_summary()["resources"]
                self.assertEqual(set(resources), set(RESOURCE_NAMES.values()))
                self.assertTrue(
                    all(
                        row["requests_started"] == 0 and row["responses"] == 0
                        for row in resources.values()
                    )
                )
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


def _wait_for(predicate, message, seconds=10.0):
    """サーバー側counterは応答後に更新されるため、有界で待つ。"""
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.02)


def _diagnostic_server(directory):
    source = Path(directory)
    (source / "index.html").write_bytes(b"<head></head>")
    for url_path in RESOURCE_NAMES:
        if url_path.startswith(("/js/", "/style/")):
            path = source / url_path.lstrip("/")
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(("fixture " + url_path).encode())
    server = LoopbackStaticServer()
    server.post_failure_render_diagnostics = True
    server.start(source)
    return server


class StartupCorrelationServerTests(unittest.TestCase):
    """Issue #791 T4-T7: 接続counter・phase別失敗・無効時の同一性。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def snapshot(self, server):
        return server.correlation_snapshot()["server"]

    def test_connection_closed_without_a_request_line_is_counted_separately(self):
        server = _diagnostic_server(self.directory.name)
        self.addCleanup(server.close)
        port = urlsplit(server.url).port
        with socket.create_connection(("127.0.0.1", port), timeout=2) as held:
            _wait_for(
                lambda: self.snapshot(server)["connections_awaiting_request_line"] == 1,
                "the held connection was not counted as awaiting",
            )
            snapshot = self.snapshot(server)
            self.assertEqual(snapshot["connections_accepted"], 1)
            self.assertEqual(snapshot["connections_without_request_line"], 0)
            held.shutdown(socket.SHUT_RDWR)
        _wait_for(
            lambda: self.snapshot(server)["connections_without_request_line"] == 1,
            "the closed connection was not counted",
        )
        snapshot = self.snapshot(server)
        self.assertEqual(snapshot["connections_awaiting_request_line"], 0)
        self.assertEqual(snapshot["requests_parsed"], 0)
        self.assertEqual(snapshot["request_line_errors"], 0)
        self.assertEqual(snapshot["requests_in_flight"], 0)

    def test_malformed_request_line_and_unlisted_get_are_counted_by_kind(self):
        server = _diagnostic_server(self.directory.name)
        self.addCleanup(server.close)
        port = urlsplit(server.url).port
        with socket.create_connection(("127.0.0.1", port), timeout=2) as bad:
            bad.sendall(b"NOT-HTTP\r\n\r\n")
            bad.recv(4096)
        with self.assertRaises(URLError) as missing:
            urlopen(server.url + "favicon.ico", timeout=2)
        missing.exception.close()
        _wait_for(
            lambda: (
                self.snapshot(server)["requests_parsed"] == 1
                and self.snapshot(server)["request_line_errors"] == 1
                and self.snapshot(server)["requests_in_flight"] == 0
            ),
            "request kinds were not settled",
        )
        snapshot = self.snapshot(server)
        self.assertEqual(snapshot["connections_accepted"], 2)
        self.assertEqual(snapshot["connections_without_request_line"], 0)
        # allowlist外のGETは要求行の統計だけを増やし、資源行は増やさない。
        self.assertTrue(
            all(row["requests_started"] == 0 for row in snapshot["resources"].values())
        )

    def test_connection_balance_holds_after_mixed_traffic(self):
        server = _diagnostic_server(self.directory.name)
        self.addCleanup(server.close)
        port = urlsplit(server.url).port
        with urlopen(server.url + "js/grid.js", timeout=2) as response:
            response.read()
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        _wait_for(
            lambda: (
                self.snapshot(server)["requests_parsed"] == 1
                and self.snapshot(server)["connections_without_request_line"] == 1
            ),
            "mixed traffic was not settled",
        )
        snapshot = self.snapshot(server)
        self.assertEqual(
            snapshot["connections_accepted"],
            snapshot["connections_without_request_line"]
            + snapshot["connections_awaiting_request_line"]
            + snapshot["request_line_errors"]
            + snapshot["requests_parsed"],
        )

    def handler_type(self, server):
        # 実socketを開かず、診断有効のhandler classだけを取り出す。
        with patch(
            "tools.reference_game_2048_launcher.ReferenceGameHTTPServer",
            return_value=Mock(server_address=("127.0.0.1", 1)),
        ) as http:
            with patch("tools.reference_game_2048_launcher.threading.Thread"):
                source = Path(self.directory.name)
                (source / "index.html").write_bytes(b"<head></head>")
                server.start(source)
        return http.call_args.args[1].func

    def failing_handler(self, handler_type, phase, error=OSError):
        handler = object.__new__(handler_type)
        handler.path = "/js/html_actuator.js"
        handler.wfile = Mock()

        def send_head():
            handler._resource_phase = phase
            handler._response_status = 200
            if phase == "body":
                handler.wfile.write.side_effect = error()
                return io.BytesIO(b"x" * 10)
            raise error()

        handler.send_head = send_head
        return handler

    def diagnostic_server_object(self):
        server = LoopbackStaticServer()
        server.post_failure_render_diagnostics = True
        return server

    def test_phase_specific_failures_keep_times_and_classify_by_boundary(self):
        from test_reference_startup_correlation import make_record
        from tools.reference_startup_correlation import classify_startup_failure

        server = self.diagnostic_server_object()
        handler_type = self.handler_type(server)
        for phase in ("headers", "body"):
            with self.assertRaises(OSError):
                self.failing_handler(handler_type, phase).do_GET()
        row = server.resource_response_summary()["resources"]["html_actuator_js"]
        self.assertEqual(row["requests_started"], 2)
        self.assertEqual(row["failed_headers_phase"], 1)
        self.assertEqual(row["failed_body_phase"], 1)
        self.assertEqual(row["failed_request_phase"], 0)
        self.assertIsNotNone(row["first_failed_ms"])
        self.assertIsNone(row["first_completed_ms"])
        result = classify_startup_failure(make_record({"html_actuator_js": row}))
        self.assertEqual(result["category"], "server_failed_before_headers")
        self.assertIn("server_failed_in_body", result["all_matching"])

    def test_body_only_failure_classifies_as_server_failed_in_body(self):
        from test_reference_startup_correlation import make_record
        from tools.reference_startup_correlation import classify_startup_failure

        server = self.diagnostic_server_object()
        handler_type = self.handler_type(server)
        with self.assertRaises(OSError):
            self.failing_handler(handler_type, "body").do_GET()
        row = server.resource_response_summary()["resources"]["html_actuator_js"]
        self.assertEqual(row["failed_body_phase"], 1)
        result = classify_startup_failure(make_record({"html_actuator_js": row}))
        self.assertEqual(result["category"], "server_failed_in_body")

    def test_clock_and_counter_exceptions_keep_the_original_send_error(self):
        server = self.diagnostic_server_object()
        handler_type = self.handler_type(server)
        with patch.object(
            ResourceResponseCounts, "_now_ms", side_effect=MemoryError("clock")
        ):
            with self.assertRaises(BrokenPipeError):
                self.failing_handler(handler_type, "headers", BrokenPipeError).do_GET()
        self.assertTrue(server.resource_response_summary()["recording_incomplete"])

    def test_diagnostic_zero_initialisation_and_expected_digests(self):
        server = _diagnostic_server(self.directory.name)
        self.addCleanup(server.close)
        snapshot = server.correlation_snapshot()
        resources = snapshot["server"]["resources"]
        self.assertEqual(set(resources), set(RESOURCE_NAMES.values()))
        self.assertEqual(len(resources), 12)
        self.assertEqual(set(snapshot["expected_body_sha256"]), set(resources))
        self.assertRegex(snapshot["correlation_id"], r"^[0-9a-f]{32}$")
        for row in resources.values():
            self.assertEqual(row["requests_started"], 0)
            self.assertIsNone(row["first_started_ms"])

    def test_disabled_server_has_no_counters_and_same_bytes(self):
        source = Path(self.directory.name)
        (source / "index.html").write_bytes(b"<head></head>")
        (source / "js").mkdir()
        (source / "js" / "grid.js").write_bytes(b"grid")
        bodies = []
        for enabled in (False, True):
            server = LoopbackStaticServer()
            server.post_failure_render_diagnostics = enabled
            url = server.start(source)
            try:
                self.assertIs(type(server._server), ReferenceGameHTTPServer)
                self.assertEqual(server._server.request_queue_size, 32)
                self.assertEqual("process_request" in vars(server._server), enabled)
                with urlopen(url + "js/grid.js", timeout=2) as response:
                    bodies.append((response.status, response.read()))
                if not enabled:
                    self.assertIsNone(server.correlation_snapshot())
                    self.assertEqual(
                        server.resource_response_summary(), {"status": "not_enabled"}
                    )
            finally:
                server.close()
        self.assertEqual(bodies[0], bodies[1])

    def test_each_start_gets_a_new_correlation_id(self):
        source = Path(self.directory.name)
        (source / "index.html").write_bytes(b"<head></head>")
        server = LoopbackStaticServer()
        server.post_failure_render_diagnostics = True
        identifiers = []
        for _ in range(2):
            server.start(source)
            identifiers.append(server.correlation_snapshot()["correlation_id"])
            server.close()
        self.assertNotEqual(*identifiers)


class ConnectionEventTests(unittest.TestCase):
    def test_unknown_connection_event_latches_recording_incomplete(self):
        counts = ResourceResponseCounts(correlation=True)
        counts.connection_event("accepted")
        self.assertFalse(counts.snapshot()["recording_incomplete"])
        counts.connection_event("unexpected")
        snapshot = counts.snapshot()
        self.assertTrue(snapshot["recording_incomplete"])
        self.assertEqual(snapshot["connections_accepted"], 1)
        self.assertEqual(snapshot["connections_awaiting_request_line"], 1)

    def test_connection_counters_are_absent_without_correlation(self):
        counts = ResourceResponseCounts()
        counts.connection_event("accepted")
        self.assertNotIn("connections_accepted", counts.snapshot())
