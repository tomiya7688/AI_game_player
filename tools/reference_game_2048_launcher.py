"""Launch the locked 2048 game in an isolated, identifiable Windows browser window."""

from __future__ import annotations

import ctypes
from functools import partial
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .reference_game_2048_source import PinnedGameSource


WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 850
WINDOW_LEFT = 60
WINDOW_TOP = 60
WINDOW_WAIT_SECONDS = 25.0


class LoopbackStaticServer:
    """Serve one materialized game directory on a private, ephemeral localhost port."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("local game server has not started")
        return self._url

    def start(self, source_path: Path) -> str:
        if self._server is not None:
            raise RuntimeError("local game server is already running")
        if not source_path.is_dir() or not (source_path / "index.html").is_file():
            raise FileNotFoundError(f"2048 source directory has no index.html: {source_path}")

        class QuietRequestHandler(SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

        handler = partial(QuietRequestHandler, directory=str(source_path))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, name="2048-static-server", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            server.server_close()
            raise
        self._server = server
        self._thread = thread
        self._url = f"http://127.0.0.1:{server.server_address[1]}/"
        return self._url

    def close(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        self._url = None
        if server is None:
            return
        try:
            if thread is not None and thread.is_alive():
                server.shutdown()
        finally:
            server.server_close()
            if thread is not None:
                thread.join(timeout=3.0)


class BrowserWindowSession:
    """Own the isolated browser process, profile, and Windows target window."""

    def __init__(self, browser_path: Path, source_path: Path) -> None:
        self.browser_path = browser_path.resolve()
        self.source_path = source_path
        self.process: subprocess.Popen[bytes] | None = None
        self.window_info = None
        self.window_is_foreground: bool | None = None
        self._profile: tempfile.TemporaryDirectory[str] | None = None

    def start(self, url: str, timeout_seconds: float = WINDOW_WAIT_SECONDS) -> None:
        if os.name != "nt":
            raise RuntimeError("browser window startup requires Windows")
        if timeout_seconds <= 0:
            raise ValueError("browser window timeout must be positive")
        if not self.browser_path.is_file():
            raise FileNotFoundError(f"browser executable was not found: {self.browser_path}")
        try:
            self._profile = tempfile.TemporaryDirectory(prefix="kadoka-2048-browser-")
            arguments = [
                str(self.browser_path),
                f"--app={url}",
                f"--user-data-dir={self._profile.name}",
                f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
                f"--window-position={WINDOW_LEFT},{WINDOW_TOP}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-background-mode",
            ]
            self.process = subprocess.Popen(
                arguments,
                cwd=self.source_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            self.window_info = self._wait_for_window(timeout_seconds)
            self.window_is_foreground = focus_resize_browser_window(self.window_info.handle, self.process.pid)
        except Exception:
            self.close()
            raise

    def wait_until_closed(self, poll_seconds: float = 0.25) -> int:
        if self.process is None:
            raise RuntimeError("browser session has not started")
        while self.process.poll() is None:
            time.sleep(poll_seconds)
        return int(self.process.returncode or 0)

    def close(self) -> None:
        try:
            self._stop_process_tree()
        finally:
            if self._profile is not None:
                self._profile.cleanup()
                self._profile = None
            self.window_info = None
            self.window_is_foreground = None

    def _wait_for_window(self, timeout_seconds: float):
        if self.process is None:
            raise RuntimeError("browser process was not created")
        from ai_game_player.window_selector import WindowsWindowSelector

        deadline = time.monotonic() + timeout_seconds
        selector = WindowsWindowSelector()
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"browser exited during startup with code {self.process.returncode}")
            for window in selector.list_windows():
                if window.process_id == self.process.pid and "2048" in window.title.casefold():
                    return window
            time.sleep(0.1)
        raise TimeoutError(
            "the 2048 browser window was not found under the launched process ID; "
            "check the browser executable and Windows desktop session"
        )

    def _stop_process_tree(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


class ReferenceGame2048Launcher:
    """Compose source verification, local serving, and browser-window lifecycle for 2048."""

    def __init__(self, cache_root: Path, browser_path: Path | None = None) -> None:
        self.cache_root = cache_root
        self.browser_path = browser_path
        self.source_path: Path | None = None
        self.server = LoopbackStaticServer()
        self.browser: BrowserWindowSession | None = None

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def window_info(self):
        return self.browser.window_info if self.browser is not None else None

    @property
    def browser_process(self) -> subprocess.Popen[bytes] | None:
        return self.browser.process if self.browser is not None else None

    @property
    def window_is_foreground(self) -> bool | None:
        return self.browser.window_is_foreground if self.browser is not None else None

    def start(self, window_wait_seconds: float = WINDOW_WAIT_SECONDS) -> None:
        if os.name != "nt":
            raise RuntimeError("the 2048 reference-game launcher requires Windows")
        try:
            self.source_path = PinnedGameSource(self.cache_root).materialize()
            browser_path = self.browser_path or find_browser_executable()
            self.server.start(self.source_path)
            self.browser = BrowserWindowSession(browser_path, self.source_path)
            self.browser.start(self.server.url, window_wait_seconds)
        except Exception:
            self.close()
            raise

    def wait_until_closed(self) -> int:
        if self.browser is None:
            raise RuntimeError("2048 launcher has not started its browser")
        return self.browser.wait_until_closed()

    def close(self) -> None:
        if self.browser is not None:
            browser = self.browser
            self.browser = None
            try:
                browser.close()
            finally:
                self.server.close()
        else:
            self.server.close()

    def __enter__(self) -> ReferenceGame2048Launcher:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def find_browser_executable() -> Path:
    """Choose the first installed Edge or Chrome executable from known Windows locations."""
    roots = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    ]
    browser_locations = (
        ("msedge", Path("Microsoft/Edge/Application/msedge.exe")),
        ("chrome", Path("Google/Chrome/Application/chrome.exe")),
    )
    for command, relative_path in browser_locations:
        found = shutil.which(command)
        if found:
            return Path(found)
        for root in roots:
            if root is None:
                continue
            candidate = Path(root) / relative_path
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        "Microsoft Edge or Google Chrome was not found; install one or pass its executable path with --browser"
    )


def focus_resize_browser_window(window_handle: int, expected_process_id: int) -> bool:
    """Restore, size, and foreground only the window owned by the launched browser process."""
    if os.name != "nt":
        raise RuntimeError("browser window focus and sizing require Windows")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int

    handle = wintypes.HWND(window_handle)
    actual_process_id = wintypes.DWORD()
    if not user32.IsWindow(handle) or not user32.GetWindowThreadProcessId(handle, ctypes.byref(actual_process_id)):
        raise RuntimeError("the selected 2048 browser window is no longer valid")
    if actual_process_id.value != expected_process_id:
        raise RuntimeError("the selected 2048 browser window belongs to a different process")
    screen_width = user32.GetSystemMetrics(0)
    screen_height = user32.GetSystemMetrics(1)
    width = min(WINDOW_WIDTH, screen_width - 2 * WINDOW_LEFT)
    height = min(WINDOW_HEIGHT, screen_height - 2 * WINDOW_TOP)
    if width < 640 or height < 480:
        raise RuntimeError("the Windows desktop must be at least 640×480 to display the 2048 game")
    left = min(WINDOW_LEFT, screen_width - width)
    top = min(WINDOW_TOP, screen_height - height)
    user32.ShowWindow(handle, 9)
    if not user32.MoveWindow(handle, left, top, width, height, True):
        raise ctypes.WinError(ctypes.get_last_error())
    user32.SetForegroundWindow(handle)
    return user32.GetForegroundWindow() == handle
