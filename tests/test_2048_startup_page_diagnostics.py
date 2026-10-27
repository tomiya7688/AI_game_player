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
                missing_page.exception.close()
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
