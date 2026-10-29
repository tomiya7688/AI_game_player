from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler
import threading
import tempfile
import socket
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from tools import reference_game_2048_launcher as launcher_module
from tools.reference_game_2048_launcher import (
    LoopbackStaticServer,
    ReferenceGame2048Launcher,
)


class PageResponseDiagnosticsTests(unittest.TestCase):
    def test_counts_page_get_status_without_logging_queries_or_other_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            index = source / "index.html"
            index.write_text("<title>2048</title>", encoding="utf-8")
            server = LoopbackStaticServer()
            try:
                url = server.start(source)
                self.assertEqual(
                    server.page_response_summary(),
                    "game_page_http_200=0, game_page_http_other=0",
                )
                for suffix in ("", "index.html?private-query=do-not-log"):
                    with urlopen(url + suffix, timeout=3) as response:
                        self.assertEqual(response.read(), b"<title>2048</title>")
                with urlopen(Request(url, method="HEAD"), timeout=3) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as missing_asset:
                    urlopen(url + "missing-asset", timeout=3)
                missing_asset.exception.close()
                index.unlink()
                with self.assertRaises(HTTPError) as missing_page:
                    urlopen(url + "index.html?another-private-query", timeout=3)
                self.assertEqual(missing_page.exception.code, 404)
                missing_page.exception.close()
                self.assertEqual(
                    server.page_response_summary(),
                    "game_page_http_200=2, game_page_http_other=1",
                )
                port = int(url.split(":")[-1].strip("/"))
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=3
                ) as connection:
                    connection.sendall(b"GET / HTTP/9.9\r\n\r\n")
                    self.assertIn(b"505", connection.recv(4096))
            finally:
                server.close()
            self.assertEqual(
                server.page_response_summary(),
                "game_page_http_200=2, game_page_http_other=1",
            )
            index.write_text("2048", encoding="utf-8")
            try:
                server.start(source)
                self.assertEqual(
                    server.page_response_summary(),
                    "game_page_http_200=0, game_page_http_other=0",
                )
            finally:
                server.close()

    def test_late_previous_generation_response_does_not_pollute_restart(self):
        response_started = threading.Event()
        release_response = threading.Event()
        original_send_response = SimpleHTTPRequestHandler.send_response

        def delayed_send_response(handler, code, message=None):
            original_send_response(handler, code, message)
            if not response_started.is_set():
                response_started.set()
                if not release_response.wait(timeout=10):
                    raise TimeoutError("test did not release old server handler")

        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory)
            (source_path / "index.html").write_text("2048", encoding="utf-8")
            server = LoopbackStaticServer()

            def request_page(url):
                with urlopen(url, timeout=10) as response:
                    return response.read()

            with ThreadPoolExecutor(max_workers=1) as executor:
                try:
                    old_url = server.start(source_path)
                    with patch.object(
                        SimpleHTTPRequestHandler, "send_response", delayed_send_response
                    ):
                        pending_response = executor.submit(request_page, old_url)
                        self.assertTrue(response_started.wait(timeout=5))
                        server.close()
                        new_url = server.start(source_path)
                        release_response.set()
                        self.assertEqual(pending_response.result(timeout=5), b"2048")
                    self.assertEqual(
                        server.page_response_summary(),
                        "game_page_http_200=0, game_page_http_other=0",
                    )
                    self.assertEqual(request_page(new_url), b"2048")
                    self.assertEqual(
                        server.page_response_summary(),
                        "game_page_http_200=1, game_page_http_other=0",
                    )
                finally:
                    release_response.set()
                    server.close()

    def test_concurrent_starts_publish_only_one_owned_server(self):
        start_together = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory)
            (source_path / "index.html").write_text("2048", encoding="utf-8")
            server = LoopbackStaticServer()

            def start_server():
                start_together.wait(timeout=5)
                try:
                    return server.start(source_path)
                except RuntimeError as error:
                    return error

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    attempts = [executor.submit(start_server) for _ in range(2)]
                    results = [attempt.result(timeout=5) for attempt in attempts]
                urls = [result for result in results if isinstance(result, str)]
                failures = [
                    result for result in results if isinstance(result, RuntimeError)
                ]
                self.assertEqual(len(urls), 1)
                self.assertEqual(len(failures), 1)
                self.assertEqual(
                    str(failures[0]), "local game server is already running"
                )
                self.assertEqual(server.url, urls[0])
                with urlopen(urls[0], timeout=3) as response:
                    self.assertEqual(response.read(), b"2048")
                self.assertEqual(
                    server.page_response_summary(),
                    "game_page_http_200=1, game_page_http_other=0",
                )
            finally:
                server.close()

    def test_browser_timeout_keeps_identity_and_counts_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory)
            (source_path / "index.html").write_text("2048", encoding="utf-8")
            launcher = ReferenceGame2048Launcher(source_path, Path("browser.exe"))
            error = TimeoutError(
                "launched_pid=8500, owned_titled_windows=3, other_2048_windows=0"
            )
            browser = Mock()

            def request_game_page_then_timeout(game_url, _window_wait_seconds):
                with urlopen(game_url, timeout=3) as response:
                    self.assertEqual(response.read(), b"2048")
                raise error

            browser.start.side_effect = request_game_page_then_timeout
            try:
                with (
                    patch(
                        "tools.reference_game_2048_launcher.os",
                        SimpleNamespace(name="nt"),
                    ),
                    patch(
                        "tools.reference_game_2048_launcher.PinnedGameSource"
                    ) as source,
                    patch(
                        "tools.reference_game_2048_launcher.BrowserWindowSession",
                        return_value=browser,
                    ),
                ):
                    self.assertIsNot(launcher_module.os, os)
                    source.return_value.materialize.return_value = source_path
                    with self.assertRaises(TimeoutError) as caught:
                        launcher.start()
                self.assertIs(caught.exception, error)
                self.assertIn("owned_titled_windows=3", str(error))
                self.assertIn(
                    "game_page_http_200=1, game_page_http_other=0", str(error)
                )
                self.assertNotIn(browser.start.call_args.args[0], str(error))
                browser.close.assert_called_once()
                self.assertIsNone(launcher.browser)
                with self.assertRaises(RuntimeError):
                    _ = launcher.server.url
            finally:
                launcher.close()

    def test_other_startup_error_is_not_replaced_or_rewritten(self):
        launcher = ReferenceGame2048Launcher(Path("unused-cache"), Path("browser.exe"))
        error = RuntimeError("browser exited during startup")
        with (
            patch("tools.reference_game_2048_launcher.os", SimpleNamespace(name="nt")),
            patch(
                "tools.reference_game_2048_launcher.PinnedGameSource", side_effect=error
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                launcher.start()
        self.assertIs(caught.exception, error)
        self.assertEqual(str(error), "browser exited during startup")

    def test_non_message_timeout_arguments_and_subclass_are_unchanged(self):
        class SpecializedTimeout(TimeoutError):
            pass

        for error in (
            TimeoutError(110, "timed out", "private-filename"),
            TimeoutError(),
            SpecializedTimeout("special"),
        ):
            with self.subTest(
                error_type=type(error).__name__, original_args=error.args
            ):
                launcher = ReferenceGame2048Launcher(
                    Path("unused-cache"), Path("browser.exe")
                )
                original_args = error.args
                original_text = str(error)
                browser = Mock()
                browser.start.side_effect = error
                with (
                    patch(
                        "tools.reference_game_2048_launcher.os",
                        SimpleNamespace(name="nt"),
                    ),
                    patch("tools.reference_game_2048_launcher.PinnedGameSource"),
                    patch(
                        "tools.reference_game_2048_launcher.BrowserWindowSession",
                        return_value=browser,
                    ),
                    patch.object(launcher.server, "start"),
                    patch.object(
                        type(launcher.server), "url", "http://127.0.0.1:9876/"
                    ),
                ):
                    with self.assertRaises(TimeoutError) as caught:
                        launcher.start()
                self.assertIs(caught.exception, error)
                self.assertEqual(error.args, original_args)
                self.assertEqual(str(error), original_text)
                browser.close.assert_called_once()
