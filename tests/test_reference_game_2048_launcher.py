import ctypes
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from unittest.mock import Mock, patch

from ai_game_player.frame_analyzer import FrameAnalyzer
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.game_2048_board import BOARD_REGION_COLORS
from ai_game_player.screen_capture import WindowsScreenCapture
from ai_game_player.windows_abi import configure_user32_prototypes
from tools.reference_game_2048_launcher import (
    HWND_TOP,
    LoopbackStaticServer,
    ReferenceGame2048Launcher,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    find_browser_executable,
    focus_resize_browser_window,
)
from tools.reference_game_2048_source import PinnedGameSource, PinnedGameSourceError


MIT_LICENSE = b"MIT fixture license\n"
INDEX_HTML = b"<title>2048</title>"
TEST_COMMIT = "a" * 40
LIVE_BOARD_RENDER_TIMEOUT_SECONDS = 15.0
LIVE_BOARD_POLL_INTERVAL_SECONDS = 0.25


class FakeArchiveResponse(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content))}


def git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def make_lock(path: Path, license_bytes: bytes = MIT_LICENSE, index_bytes: bytes = INDEX_HTML) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "game_id": "2048",
                "repository": "https://github.com/example/2048",
                "commit": TEST_COMMIT,
                "archive_url": f"https://codeload.github.com/example/2048/zip/{TEST_COMMIT}",
                "git_tree_sha": "b" * 40,
                "file_blobs": {
                    "index.html": git_blob_sha(index_bytes),
                    "LICENSE.txt": git_blob_sha(license_bytes),
                },
                "license": {
                    "spdx_id": "MIT",
                    "path": "LICENSE.txt",
                    "git_blob_sha": git_blob_sha(license_bytes),
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def make_archive(entries: dict[str, bytes]) -> bytes:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return archive_bytes.getvalue()


class PinnedGameSourceTest(unittest.TestCase):
    def test_downloads_exact_locked_archive_once_and_reuses_verified_cache(self):
        archive = make_archive(
            {
                "2048-test/index.html": INDEX_HTML,
                "2048-test/LICENSE.txt": MIT_LICENSE,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = make_lock(root / "source.lock.json")
            source = PinnedGameSource(root / "cache", lock_path)
            with patch("urllib.request.urlopen", return_value=FakeArchiveResponse(archive)) as fetch:
                materialized = source.materialize()
                cached = source.materialize()
                self.assertEqual(materialized, cached)
                self.assertEqual((materialized / "index.html").read_bytes(), b"<title>2048</title>")
                self.assertEqual(fetch.call_count, 1)
                request = fetch.call_args.args[0]
                self.assertEqual(request.full_url, f"https://codeload.github.com/example/2048/zip/{TEST_COMMIT}")
                (materialized / "index.html").write_bytes(b"changed game source")
                with self.assertRaisesRegex(PinnedGameSourceError, "does not match the lock"):
                    source.materialize()


    def test_rejects_parent_path_before_writing_outside_the_cache(self):
        archive = make_archive(
            {
                "2048-test/index.html": INDEX_HTML,
                "2048-test/LICENSE.txt": MIT_LICENSE,
                "../../escape.txt": b"outside",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = PinnedGameSource(root / "cache", make_lock(root / "source.lock.json"))
            with patch("urllib.request.urlopen", return_value=FakeArchiveResponse(archive)):
                with self.assertRaisesRegex(PinnedGameSourceError, "unsafe path"):
                    source.materialize()
            self.assertFalse((root / "escape.txt").exists())

    def test_rejects_license_bytes_that_do_not_match_the_lock(self):
        archive = make_archive(
            {
                "2048-test/index.html": INDEX_HTML,
                "2048-test/LICENSE.txt": b"changed license\n",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = PinnedGameSource(root / "cache", make_lock(root / "source.lock.json"))
            with patch("urllib.request.urlopen", return_value=FakeArchiveResponse(archive)):
                with self.assertRaisesRegex(PinnedGameSourceError, "does not match the lock"):
                    source.materialize()

    def test_invalid_existing_cache_is_preserved_without_network_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache" / TEST_COMMIT
            cache.mkdir(parents=True)
            (cache / "index.html").write_bytes(INDEX_HTML)
            (cache / "LICENSE.txt").write_bytes(b"not the locked license")
            marker = root / "keep.txt"
            marker.write_text("preserve existing cache", encoding="utf-8")
            source = PinnedGameSource(root / "cache", make_lock(root / "source.lock.json"))
            with patch("urllib.request.urlopen") as fetch:
                with self.assertRaisesRegex(PinnedGameSourceError, "does not match the lock"):
                    source.materialize()
                fetch.assert_not_called()
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserve existing cache")


class LoopbackStaticServerTest(unittest.TestCase):
    def test_serves_game_from_loopback_and_releases_port_on_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "index.html").write_text("2048 fixture", encoding="utf-8")
            server = LoopbackStaticServer()
            game_url = server.start(source)
            try:
                with urlopen(game_url, timeout=2) as response:
                    self.assertEqual(response.read(), b"2048 fixture")
                self.assertTrue(game_url.startswith("http://127.0.0.1:"))
            finally:
                server.close()
            with self.assertRaises(URLError):
                urlopen(game_url, timeout=1)


class BrowserDiscoveryTest(unittest.TestCase):
    def test_installed_edge_precedes_chrome_found_on_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edge_path = root / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            edge_path.parent.mkdir(parents=True)
            edge_path.touch()
            with (
                patch.dict(
                    os.environ,
                    {
                        "PROGRAMFILES(X86)": str(root / "x86"),
                        "PROGRAMFILES": str(root),
                        "LOCALAPPDATA": str(root / "local"),
                    },
                ),
                patch("shutil.which", side_effect=lambda command: "C:/tools/chrome.exe" if command == "chrome" else None),
            ):
                self.assertEqual(find_browser_executable(), edge_path)

    def test_owned_browser_window_reports_when_foreground_activation_is_denied(self):
        import ctypes
        from ctypes import wintypes

        window_handle = 1234
        process_id = 5678
        user32 = Mock()
        user32.IsWindow.return_value = True
        user32.GetWindowThreadProcessId.side_effect = (
            lambda _handle, output: setattr(
                ctypes.cast(output, ctypes.POINTER(wintypes.DWORD)).contents,
                "value",
                process_id,
            ) or 1
        )
        user32.GetSystemMetrics.side_effect = [1920, 1080]
        user32.ShowWindow.return_value = 1
        user32.MoveWindow.return_value = 1
        user32.SetWindowPos.return_value = 1
        user32.SetForegroundWindow.return_value = 0
        user32.GetForegroundWindow.return_value = 9876

        with patch("tools.reference_game_2048_launcher.os.name", "nt"), patch.object(
            ctypes,
            "WinDLL",
            return_value=user32,
            create=True,
        ):
            self.assertFalse(focus_resize_browser_window(window_handle, process_id))

        user32.SetWindowPos.assert_called_once()
        user32.InvalidateRect.assert_called_once()
        user32.UpdateWindow.assert_called_once()
        insert_after = user32.SetWindowPos.call_args.args[1]
        flags = user32.SetWindowPos.call_args.args[6]
        self.assertEqual(insert_after.value or 0, HWND_TOP)
        self.assertEqual(flags, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("KADOKA_RUN_2048_INTEGRATION") == "1",
    "real browser-window integration runs only in the Windows E2E workflow",
)
class WindowsReferenceGameIntegrationTest(unittest.TestCase):
    def test_launches_locked_game_in_owned_window_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            launcher = ReferenceGame2048Launcher(Path(temporary) / "source-cache")
            launcher.start()
            process = launcher.browser_process
            window = launcher.window_info
            game_url = launcher.url
            try:
                self.assertIsNotNone(process)
                self.assertIsNotNone(window)
                self.assertIn("2048", window.title.casefold())
                self.assertEqual(window.process_id, process.pid)
                self.assertIsInstance(launcher.window_is_foreground, bool)
                with urlopen(game_url, timeout=5) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("2048", html)
                capture = WindowsScreenCapture()
                try:
                    adapter = ReferenceGame2048Adapter()
                    frame_analyzer = FrameAnalyzer()
                    deadline = time.monotonic() + LIVE_BOARD_RENDER_TIMEOUT_SECONDS
                    while True:
                        frame = capture.capture(window.handle)
                        observation = frame_analyzer.analyze(frame, "live-2048")
                        adapted, candidates = adapter.adapt(frame, observation)
                        game_state = adapted.features["game_2048"]
                        if game_state["board"] is not None or time.monotonic() >= deadline:
                            break
                        time.sleep(LIVE_BOARD_POLL_INTERVAL_SECONDS)
                finally:
                    capture.close()
                failure_diagnostic = None
                if game_state["board"] is None:
                    diagnostic_directory = Path("build/e2e")
                    diagnostic_directory.mkdir(parents=True, exist_ok=True)
                    user32 = ctypes.windll.user32
                    configure_user32_prototypes(user32)
                    window_rect = (ctypes.c_long * 4)()
                    rect_available = bool(user32.GetWindowRect(window.handle, ctypes.byref(window_rect)))
                    visible_unoccluded = None
                    if rect_available:
                        left, top, right, bottom = window_rect
                        visible_unoccluded = WindowsScreenCapture._is_visible_and_unoccluded(
                            user32,
                            window.handle,
                            left,
                            top,
                            right,
                            bottom,
                        )
                    palette_counts = Counter(
                        (
                            frame.bgra[offset + 2],
                            frame.bgra[offset + 1],
                            frame.bgra[offset],
                        )
                        for offset in range(0, len(frame.bgra), 4)
                        if (
                            frame.bgra[offset + 2],
                            frame.bgra[offset + 1],
                            frame.bgra[offset],
                        )
                        in BOARD_REGION_COLORS
                    )
                    diagnostic = {
                        "width": frame.width,
                        "height": frame.height,
                        "window_title": window.title,
                        "window_is_foreground": launcher.window_is_foreground,
                        "window_rect": list(window_rect) if rect_available else None,
                        "window_visible_unoccluded": visible_unoccluded,
                        "mean_rgb": observation.features.get("mean_rgb"),
                        "palette_pixel_counts": {
                            ",".join(str(channel) for channel in rgb): count
                            for rgb, count in sorted(palette_counts.items())
                        },
                        "board_region": repr(adapter.board_recognizer._find_board_region(frame)),
                        "capture_backend_error": capture.last_backend_error,
                    }
                    failure_diagnostic = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
                    (diagnostic_directory / "2048-live-capture.json").write_text(
                        json.dumps(diagnostic, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                self.assertIsNotNone(
                    game_state["board"],
                    f"frame={frame.width}x{frame.height}, mean={observation.features.get('mean_rgb')}, "
                    f"confidence={game_state['confidence']}, foreground={launcher.window_is_foreground}, "
                    f"diagnostics={failure_diagnostic}",
                )
                self.assertEqual([candidate.label for candidate in candidates], ["UP", "DOWN", "LEFT", "RIGHT"])
            finally:
                launcher.close()
            process.wait(timeout=10)
            self.assertIsNotNone(process.poll())
            with self.assertRaises(URLError):
                urlopen(game_url, timeout=1)


if __name__ == "__main__":
    unittest.main()
