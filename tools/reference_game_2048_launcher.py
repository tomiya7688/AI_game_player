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
from tools.reference_game_seed import seeded_index
from pathlib import Path
from .reference_game_2048_source import PinnedGameSource
from .startup_failure_evidence import StartupFailureEvidence


WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 850
GAME_DEVICE_SCALE_FACTOR = 0.75
WINDOW_LEFT = 60
WINDOW_TOP = 60
WINDOW_WAIT_SECONDS = 25.0
PROFILE_CLEANUP_MAX_ATTEMPTS = 10
PROFILE_CLEANUP_RETRY_SECONDS = 0.1
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


# {
#   責務: [PageResponseCounts: 一回のローカルHTTPサーバー起動に属するページGET応答件数だけを保持する],
#   フィールド: [
#     _lock: HTTP処理スレッド間で件数の更新と参照を排他制御するロック,
#     _ok: HTTP200応答の件数,
#     _other: HTTP200以外の応答件数
#   ]
# }
class PageResponseCounts:
    """Keep only HTTP status counts for one server generation, including late handlers."""

    # {
    #   責務: [__init__: 一起動分の応答件数と専用ロックを初期化する],
    #   処理: [件数を0にし、別起動のカウンターとは共有しない],
    #   引数: [self: 初期化する応答件数の保持先],
    #   戻り値: [None: 初期化した状態をselfに保持する]
    # }
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ok = 0
        self._other = 0

    # {
    #   責務: [record: この起動に属するページGET応答を一件加算する],
    #   処理: [専用ロック内でHTTP200とそれ以外を分けて加算する],
    #   引数: [self: handlerが捕捉した起動別カウンター, status_code: 生成したHTTP応答コード],
    #   戻り値: [None: 件数だけを更新し、要求URLや本文は保存しない]
    # }
    def record(self, status_code: int) -> None:
        with self._lock:
            if status_code == 200:
                self._ok += 1
            else:
                self._other += 1

    # {
    #   責務: [summary: 同一起動のHTTP200件数と非200件数を診断文へ変換する],
    #   処理: [専用ロック内で二つの件数を読み、固定キーの文字列へまとめる],
    #   引数: [self: 参照する起動別カウンター],
    #   戻り値: [str: 応答生成件数。ブラウザーの受信や描画完了を示す値ではない]
    # }
    def summary(self) -> str:
        with self._lock:
            return f"game_page_http_200={self._ok}, game_page_http_other={self._other}"


class LoopbackStaticServer:
    """Serve one materialized game directory on a private, ephemeral localhost port."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._url: str | None = None
        self._lifecycle_lock = threading.Lock()
        self._page_responses = PageResponseCounts()
        self.test_seed: int | None = None
        self.test_seed_metadata: dict | None = None

    def page_response_summary(self) -> str:
        """Return GET-page HTTP response counts, not proof of browser receipt or rendering.

        Only counters are retained: request URLs, query values, titles and response
        bodies are never copied into this diagnostic. Counts cover one server start.
        """
        with self._lifecycle_lock:
            return self._page_responses.summary()

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("local game server has not started")
        return self._url

    def start(self, source_path: Path) -> str:
        # Publish one complete server generation before another start or close proceeds.
        with self._lifecycle_lock:
            return self._start_locked(source_path)

    def _start_locked(self, source_path: Path) -> str:
        if self._server is not None:
            raise RuntimeError("local game server is already running")
        if not source_path.is_dir() or not (source_path / "index.html").is_file():
            raise FileNotFoundError(
                f"2048 source directory has no index.html: {source_path}"
            )
        # Old daemon handlers may finish after close; they retain only their own counters.
        page_responses = PageResponseCounts()
        seeded_response, self.test_seed_metadata = seeded_index(
            source_path, self.test_seed
        )

        class QuietRequestHandler(SimpleHTTPRequestHandler):
            def do_GET(self) -> None:
                if seeded_response is not None and self.path.partition("?")[0] in (
                    "/",
                    "/index.html",
                ):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(seeded_response)))
                    self.end_headers()
                    self.wfile.write(seeded_response)
                else:
                    super().do_GET()

            def log_message(self, format: str, *args: object) -> None:
                return

            def send_response(self, code: int, message: str | None = None) -> None:
                super().send_response(code, message)
                # A response header is not evidence that the browser received or rendered it.
                if self.command == "GET" and self.path.partition("?")[0] in (
                    "/",
                    "/index.html",
                ):
                    page_responses.record(code)

        handler = partial(QuietRequestHandler, directory=str(source_path))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(
            target=server.serve_forever, name="2048-static-server", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:
            server.server_close()
            raise
        self._page_responses = page_responses
        self._server = server
        self._thread = thread
        self._url = f"http://127.0.0.1:{server.server_address[1]}/"
        return self._url

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close_locked()

    def _close_locked(self) -> None:
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

    def __init__(
        self,
        browser_path: Path,
        source_path: Path,
        *,
        failure_evidence: StartupFailureEvidence | None = None,
    ) -> None:
        self.browser_path = browser_path.resolve()
        self.source_path = source_path
        self.process: subprocess.Popen[bytes] | None = None
        self.window_info = None
        self.window_is_foreground: bool | None = None
        self._profile: tempfile.TemporaryDirectory[str] | None = None
        self.failure_evidence = failure_evidence
        self.failure_evidence_manifest: Path | None = None
        self.failure_evidence_error: str | None = None

    def start(self, url: str, timeout_seconds: float = WINDOW_WAIT_SECONDS) -> None:
        if os.name != "nt":
            raise RuntimeError("browser window startup requires Windows")
        if timeout_seconds <= 0:
            raise ValueError("browser window timeout must be positive")
        if not self.browser_path.is_file():
            raise FileNotFoundError(
                f"browser executable was not found: {self.browser_path}"
            )
        started = time.monotonic()
        phase_durations_ms: dict[str, float] = {}
        wait_started: float | None = None
        self.failure_evidence_manifest = None
        self.failure_evidence_error = None
        try:
            self._profile = tempfile.TemporaryDirectory(prefix="kadoka-2048-browser-")
            arguments = [
                str(self.browser_path),
                f"--app={url}",
                f"--user-data-dir={self._profile.name}",
                f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
                f"--window-position={WINDOW_LEFT},{WINDOW_TOP}",
                f"--force-device-scale-factor={GAME_DEVICE_SCALE_FACTOR}",
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
            phase_durations_ms["browser_spawn"] = (time.monotonic() - started) * 1000
            wait_started = time.monotonic()
            self.window_info = self._wait_for_window(timeout_seconds)
            self.window_is_foreground = focus_resize_browser_window(
                self.window_info.handle, self.process.pid
            )
        except Exception as startup_error:
            try:
                if self.failure_evidence is not None and self.process is not None:
                    try:
                        phase_durations_ms["startup_total"] = (
                            time.monotonic() - started
                        ) * 1000
                        if wait_started is not None:
                            phase_durations_ms["window_wait"] = (
                                time.monotonic() - wait_started
                            ) * 1000
                        self.failure_evidence_manifest = self.failure_evidence.save(
                            self.process.pid, phase_durations_ms
                        )
                    except Exception as evidence_error:
                        # Diagnostics must never replace the original startup failure.
                        self.failure_evidence_error = type(evidence_error).__name__
            finally:
                # Capture interruption must not bypass owned browser/profile cleanup either.
                try:
                    self.close()
                except Exception:
                    raise startup_error
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
            try:
                self._cleanup_profile()
            finally:
                self.window_info = None
                self.window_is_foreground = None

    def _cleanup_profile(self) -> None:
        """Release this session's profile, allowing only bounded permission-lock retries."""
        if self._profile is None:
            return
        for attempt in range(PROFILE_CLEANUP_MAX_ATTEMPTS):
            try:
                self._profile.cleanup()
            except PermissionError:
                # Windowsの終了直後に残る一時ロックだけを待つ。最終失敗は隠さない。
                if attempt + 1 == PROFILE_CLEANUP_MAX_ATTEMPTS:
                    raise
                time.sleep(PROFILE_CLEANUP_RETRY_SECONDS)
            else:
                self._profile = None
                return

    def _wait_for_window(self, timeout_seconds: float):
        if self.process is None:
            raise RuntimeError("browser process was not created")
        from ai_game_player.window_selector import WindowsWindowSelector

        deadline = time.monotonic() + timeout_seconds
        selector = WindowsWindowSelector()
        owned_titled_window_count = 0
        other_game_window_count = 0
        while time.monotonic() < deadline:
            exit_code = self.process.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"browser exited during startup: launched_pid={self.process.pid}, exit_code={exit_code}"
                )
            owned_titled_window_count = 0
            other_game_window_count = 0
            for window in selector.list_windows():
                if not window.title:
                    continue
                is_game_title = "2048" in window.title.casefold()
                if window.process_id == self.process.pid:
                    owned_titled_window_count += 1
                    if is_game_title:
                        return window
                elif is_game_title:
                    other_game_window_count += 1
            time.sleep(0.1)
        # 別アプリのタイトルは記録せず、直近の列挙結果の件数だけでPID不一致を切り分ける。
        raise TimeoutError(
            "the 2048 browser window was not found under the launched process ID; "
            "check the browser executable and Windows desktop session; "
            f"launched_pid={self.process.pid}, owned_titled_windows={owned_titled_window_count}, "
            f"other_2048_windows={other_game_window_count}"
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

    def __init__(
        self,
        cache_root: Path,
        browser_path: Path | None = None,
        *,
        failure_evidence: StartupFailureEvidence | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.browser_path = browser_path
        self.source_path: Path | None = None
        self.server = LoopbackStaticServer()
        self.browser: BrowserWindowSession | None = None
        self.failure_evidence = failure_evidence

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
            self.browser.failure_evidence = self.failure_evidence
            self.browser.start(self.server.url, window_wait_seconds)
        except Exception as startup_error:
            if (
                self.browser is not None
                and type(startup_error) is TimeoutError
                and len(startup_error.args) == 1
                and isinstance(startup_error.args[0], str)
            ):
                # Only the window wait's plain-message exception may gain log context.
                # Preserve errno/filename arguments and custom exception contracts untouched.
                startup_error.args = (
                    f"{startup_error}; {self.server.page_response_summary()}",
                )
                if self.browser.failure_evidence_manifest is not None:
                    startup_error.args = (
                        f"{startup_error}; startup_evidence={self.browser.failure_evidence_manifest}",
                    )
                elif self.browser.failure_evidence_error is not None:
                    startup_error.args = (
                        f"{startup_error}; startup_evidence_error={self.browser.failure_evidence_error}",
                    )
            try:
                self.close()
            except Exception:
                raise startup_error
            raise

    def wait_until_closed(self) -> int:
        if self.browser is None:
            raise RuntimeError("2048 launcher has not started its browser")
        return self.browser.wait_until_closed()

    def close(self) -> None:
        try:
            if self.browser is not None:
                # cleanup失敗時も所有者を残し、利用者がcloseを再実行できるようにする。
                self.browser.close()
                self.browser = None
        finally:
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
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.MoveWindow.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
    ]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
    user32.InvalidateRect.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int

    handle = wintypes.HWND(window_handle)
    actual_process_id = wintypes.DWORD()
    if not user32.IsWindow(handle) or not user32.GetWindowThreadProcessId(
        handle, ctypes.byref(actual_process_id)
    ):
        raise RuntimeError("the selected 2048 browser window is no longer valid")
    if actual_process_id.value != expected_process_id:
        raise RuntimeError(
            "the selected 2048 browser window belongs to a different process"
        )
    screen_width = user32.GetSystemMetrics(0)
    screen_height = user32.GetSystemMetrics(1)
    width = min(WINDOW_WIDTH, screen_width - 2 * WINDOW_LEFT)
    height = min(WINDOW_HEIGHT, screen_height - 2 * WINDOW_TOP)
    if width < 640 or height < 480:
        raise RuntimeError(
            "the Windows desktop must be at least 640×480 to display the 2048 game"
        )
    left = min(WINDOW_LEFT, screen_width - width)
    top = min(WINDOW_TOP, screen_height - height)
    user32.ShowWindow(handle, 9)
    if not user32.MoveWindow(handle, left, top, width, height, True):
        raise ctypes.WinError(ctypes.get_last_error())
    z_order_flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
    if not user32.SetWindowPos(
        handle, wintypes.HWND(HWND_TOP), 0, 0, 0, 0, z_order_flags
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.InvalidateRect(handle, None, True) or not user32.UpdateWindow(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    user32.SetForegroundWindow(handle)
    return user32.GetForegroundWindow() == window_handle
