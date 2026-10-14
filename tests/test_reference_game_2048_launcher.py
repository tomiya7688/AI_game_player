import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from unittest.mock import patch

from tools.reference_game_2048_launcher import (
    LoopbackStaticServer,
    ReferenceGame2048Launcher,
    find_browser_executable,
)
from tools.reference_game_2048_source import PinnedGameSource, PinnedGameSourceError


MIT_LICENSE = b"MIT fixture license\n"
INDEX_HTML = b"<title>2048</title>"
TEST_COMMIT = "a" * 40


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
            finally:
                launcher.close()
            process.wait(timeout=10)
            self.assertIsNotNone(process.poll())
            with self.assertRaises(URLError):
                urlopen(game_url, timeout=1)


if __name__ == "__main__":
    unittest.main()
