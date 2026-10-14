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
from ai_game_player.game_2048_board import (
    BOARD_REGION_COLORS,
    FRAME_BYTES_PER_PIXEL,
    MAXIMUM_BOARD_REGION_COLOR_DISTANCE_SQUARED,
    MAXIMUM_BOARD_ROW_COLOR_GAP_PIXELS,
    MAXIMUM_ROW_GROUP_Y_DELTA,
    MINIMUM_ROW_MATCH_PIXELS,
    MINIMUM_ROW_MATCH_WIDTH_FRACTION,
    MINIMUM_ROW_SPAN_PIXELS,
    MINIMUM_ROW_SPAN_WIDTH_FRACTION,
)
from ai_game_player.screen_capture import ScreenFrame, WindowsScreenCapture
from ai_game_player.windows_abi import configure_user32_prototypes
from tools.reference_game_2048_launcher import (
    HWND_TOP,
    GAME_DEVICE_SCALE_FACTOR,
    BrowserWindowSession,
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


def board_row_group_diagnostics(
    frame: ScreenFrame,
    nearest_distance_by_rgb: dict[tuple[int, int, int], int],
) -> dict[str, object]:
    minimum_row_pixels = max(
        MINIMUM_ROW_MATCH_PIXELS,
        round(frame.width * MINIMUM_ROW_MATCH_WIDTH_FRACTION),
    )
    minimum_row_span = max(
        MINIMUM_ROW_SPAN_PIXELS,
        round(frame.width * MINIMUM_ROW_SPAN_WIDTH_FRACTION),
    )
    matched_rows = []
    for y in range(frame.height):
        matching_x_positions = []
        row_offset = y * frame.width * FRAME_BYTES_PER_PIXEL
        for x in range(frame.width):
            pixel_offset = row_offset + x * FRAME_BYTES_PER_PIXEL
            rgb = (
                frame.bgra[pixel_offset + 2],
                frame.bgra[pixel_offset + 1],
                frame.bgra[pixel_offset],
            )
            if nearest_distance_by_rgb[rgb] > MAXIMUM_BOARD_REGION_COLOR_DISTANCE_SQUARED:
                continue
            matching_x_positions.append(x)
        if not matching_x_positions:
            continue
        row_color_spans = []
        span_left = matching_x_positions[0]
        span_right = span_left
        span_pixel_count = 1
        for x in matching_x_positions[1:]:
            if x - span_right - 1 > MAXIMUM_BOARD_ROW_COLOR_GAP_PIXELS:
                row_color_spans.append((span_left, span_right + 1, span_pixel_count))
                span_left = x
                span_pixel_count = 0
            span_right = x
            span_pixel_count += 1
        row_color_spans.append((span_left, span_right + 1, span_pixel_count))
        left, right, matching_pixels = max(
            row_color_spans,
            key=lambda span: (span[2], span[1] - span[0]),
        )
        if matching_pixels >= minimum_row_pixels and right - left >= minimum_row_span:
            matched_rows.append((y, left, right, matching_pixels))

    row_groups = []
    for row in matched_rows:
        if row_groups and row[0] <= row_groups[-1][-1][0] + MAXIMUM_ROW_GROUP_Y_DELTA:
            row_groups[-1].append(row)
        else:
            row_groups.append([row])

    group_summaries = []
    for group in row_groups:
        top = group[0][0]
        bottom = group[-1][0] + 1
        left = min(row[1] for row in group)
        right = max(row[2] for row in group)
        width = right - left
        height = bottom - top
        side = max(width, height)
        shape_confidence = 1.0 - abs(width - height) / side if side else 0.0
        density = sum(row[3] for row in group) / (width * height) if width and height else 0.0
        group_summaries.append(
            {
                "bounds": [left, top, right, bottom],
                "row_count": len(group),
                "shape_confidence": round(shape_confidence, 4),
                "color_density": round(density, 4),
            }
        )
    group_summaries.sort(key=lambda summary: summary["row_count"], reverse=True)
    return {
        "matched_row_count": len(matched_rows),
        "group_count": len(row_groups),
        "largest_groups": group_summaries[:10],
    }


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

    def test_browser_window_uses_the_configured_game_device_scale_factor(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        profile = Mock(name="browser_profile")
        profile.name = "isolated-profile"
        process = Mock(pid=5678)

        with (
            patch("tools.reference_game_2048_launcher.os.name", "nt"),
            patch.object(Path, "is_file", return_value=True),
            patch("tools.reference_game_2048_launcher.tempfile.TemporaryDirectory", return_value=profile),
            patch("tools.reference_game_2048_launcher.subprocess.Popen", return_value=process) as start_browser,
            patch.object(session, "_wait_for_window", return_value=Mock()),
            patch("tools.reference_game_2048_launcher.focus_resize_browser_window", return_value=True),
        ):
            session.start("http://127.0.0.1:8000/")

        browser_arguments = start_browser.call_args.args[0]
        self.assertIn(
            f"--force-device-scale-factor={GAME_DEVICE_SCALE_FACTOR}",
            browser_arguments,
        )
        session.close()


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("KADOKA_RUN_2048_INTEGRATION") == "1",
    "real browser-window integration runs only in the Windows E2E workflow",
)
class WindowsReferenceGameIntegrationTest(unittest.TestCase):
    def test_launches_locked_game_in_owned_window_and_cleans_up(self):
        print("2048 integration: test body started", flush=True)
        with tempfile.TemporaryDirectory() as temporary:
            print("2048 integration: temporary workspace created", flush=True)
            launcher = ReferenceGame2048Launcher(Path(temporary) / "source-cache")
            print("2048 integration: starting pinned source and browser launcher", flush=True)
            launcher.start()
            print("2048 integration: pinned source and browser launcher started", flush=True)
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
                print("2048 integration: loopback server returned the pinned game page", flush=True)
                capture = WindowsScreenCapture()
                try:
                    adapter = ReferenceGame2048Adapter()
                    frame_analyzer = FrameAnalyzer()
                    deadline = time.monotonic() + LIVE_BOARD_RENDER_TIMEOUT_SECONDS
                    capture_attempt = 0
                    while True:
                        capture_attempt += 1
                        if capture_attempt == 1:
                            print("2048 integration: starting HWND frame capture", flush=True)
                        frame = capture.capture(window.handle)
                        if capture_attempt == 1:
                            print(
                                f"2048 integration: HWND frame captured at {frame.width}x{frame.height}; "
                                f"backend_error={capture.last_backend_error!r}",
                                flush=True,
                            )
                        observation = frame_analyzer.analyze(frame, "live-2048")
                        adapted, candidates = adapter.adapt(frame, observation)
                        game_state = adapted.features["game_2048"]
                        if game_state["board"] is not None or time.monotonic() >= deadline:
                            break
                        time.sleep(LIVE_BOARD_POLL_INTERVAL_SECONDS)
                finally:
                    capture.close()
                    print("2048 integration: HWND capture resources closed", flush=True)
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
                    frame_color_counts = Counter(
                        (
                            frame.bgra[offset + 2],
                            frame.bgra[offset + 1],
                            frame.bgra[offset],
                        )
                        for offset in range(0, len(frame.bgra), 4)
                    )
                    palette_counts = Counter(
                        {
                            rgb: count
                            for rgb, count in frame_color_counts.items()
                            if rgb in BOARD_REGION_COLORS
                        }
                    )
                    distance_thresholds = (36, 144, 324, 900)
                    distance_bucket_counts = Counter()
                    nearest_distance_by_rgb = {}
                    for rgb, count in frame_color_counts.items():
                        nearest_distance = min(
                            sum((actual - expected) ** 2 for actual, expected in zip(rgb, palette_rgb))
                            for palette_rgb in BOARD_REGION_COLORS
                        )
                        nearest_distance_by_rgb[rgb] = nearest_distance
                        bucket = next(
                            (str(threshold) for threshold in distance_thresholds if nearest_distance <= threshold),
                            "over_900",
                        )
                        distance_bucket_counts[bucket] += count
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
                        "nearest_palette_distance_pixel_counts": dict(sorted(distance_bucket_counts.items())),
                        "board_region": repr(adapter.board_recognizer._find_board_region(frame)),
                        "board_row_groups": board_row_group_diagnostics(frame, nearest_distance_by_rgb),
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
