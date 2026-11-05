"""Windows CI用の実入力センチネル。user32の入力APIを、テスト自身が作った窓宛て以外で遮断する。

CIのWindows platform gate（Issue #94）は、テストプロセス内のuser32呼出しをこのセンチネル経由に
置き換える。グローバル入力API（SendInput、keybd_event、mouse_event、SetCursorPos等）は常に
呼び出し前に遮断し、キーボード／マウスのwindow messageは送信先HWNDがテストプロセス自身の
所有でない場合に遮断する。遮断した呼出しは例外を送出するだけでなく記録し、テストが例外を
握りつぶしてもgateを失敗させる。子プロセスのuser32は対象外（gateの対象テストに含めない）。
"""

from __future__ import annotations

import ctypes
import os
import threading
from dataclasses import dataclass, replace
from pathlib import PurePath
from typing import Any, Callable

GLOBAL_INPUT_FUNCTIONS = frozenset(
    {
        "SendInput",
        "keybd_event",
        "mouse_event",
        "SetCursorPos",
        "SetPhysicalCursorPos",
        "BlockInput",
        "InjectTouchInput",
        "InjectSyntheticPointerInput",
        "InjectMouseInput",
        "InjectKeyboardInput",
    }
)
WINDOW_MESSAGE_FUNCTIONS = frozenset(
    {
        "PostMessageW",
        "PostMessageA",
        "SendMessageW",
        "SendMessageA",
        "SendMessageTimeoutW",
        "SendMessageTimeoutA",
        "SendNotifyMessageW",
        "SendNotifyMessageA",
        "SendMessageCallbackW",
        "SendMessageCallbackA",
    }
)
# 第1引数がHWNDではなくthread IDのため所有窓を判定できない。入力messageは常に遮断する。
THREAD_MESSAGE_FUNCTIONS = frozenset({"PostThreadMessageW", "PostThreadMessageA"})
GUARDED_FUNCTIONS = (
    GLOBAL_INPUT_FUNCTIONS | WINDOW_MESSAGE_FUNCTIONS | THREAD_MESSAGE_FUNCTIONS
)
# WM_KEYFIRST..WM_KEYLAST と WM_MOUSEFIRST..WM_MOUSELAST。WM_PRINT等の描画要求は対象外。
KEYBOARD_MESSAGE_RANGE = range(0x0100, 0x0109 + 1)
MOUSE_MESSAGE_RANGE = range(0x0200, 0x020E + 1)
# 入力と同等に操作・状態変更を起こすmessage: WM_CLOSE、WM_INPUT、WM_COMMAND、WM_SYSCOMMAND、
# WM_GESTURE、WM_TOUCH、WM_HOTKEY、WM_APPCOMMAND。
INPUT_EQUIVALENT_MESSAGES = frozenset(
    {0x0010, 0x00FF, 0x0111, 0x0112, 0x0119, 0x0240, 0x0312, 0x0319}
)
# WM_POINTERDEVICECHANGE..WM_POINTERHWHEEL（0x0238〜0x024F、WM_TOUCH 0x0240を含む）。
POINTER_MESSAGE_RANGE = range(0x0238, 0x024F + 1)


class InputSentinelViolation(RuntimeError):
    """CI中に実OS入力となり得るuser32呼出しを遮断したことを示す。"""


@dataclass(frozen=True)
class SentinelEvent:
    """センチネルが判定した1回のuser32呼出し（function名・message・送信先・判定理由）。"""

    function: str
    message: int | None
    window_handle: int | None
    owner_process_id: int | None
    reason: str
    context: str = ""
    thread: str = ""


def _plain_int(value: Any) -> int:
    """ctypesのHWND/UINT等とPython intを同じ整数へ正規化する。Noneは0。"""
    if value is None:
        return 0
    inner = getattr(value, "value", value)
    return 0 if inner is None else int(inner)


def is_input_message(message: int) -> bool:
    """キーボード・マウス、または入力と同等に操作を起こすwindow messageならTrue。"""
    return (
        message in KEYBOARD_MESSAGE_RANGE
        or message in MOUSE_MESSAGE_RANGE
        or message in POINTER_MESSAGE_RANGE
        or message in INPUT_EQUIVALENT_MESSAGES
    )


def is_user32_library(library_name: str | None) -> bool:
    """CDLLの_nameがuser32（拡張子・path・大文字小文字を問わない）ならTrue。"""
    if not library_name:
        return False
    return PurePath(str(library_name).replace("\\", "/")).stem.lower() == "user32"


class InputSentinel:
    """user32の入力系関数を判定・遮断し、遮断と許可の記録を保持する。

    フィールド: current_process_id=許可する送信先窓の所有PID、owner_process_id_of=HWNDから所有PIDを
    返す関数（取得不能は0）、blocked/allowed=判定済み呼出し、context=記録へ添える実行中テストID、
    _lock=並列テストスレッド用の記録ロック、_original_getitem/_installed_getitem=installで差し替えた
    前後のctypes.CDLL.__getitem__。
    """

    def __init__(
        self,
        owner_process_id_of: Callable[[int], int],
        current_process_id: int | None = None,
    ) -> None:
        self.current_process_id = (
            os.getpid() if current_process_id is None else current_process_id
        )
        self.owner_process_id_of = owner_process_id_of
        self.blocked: list[SentinelEvent] = []
        self.allowed: list[SentinelEvent] = []
        self.context = ""
        self._lock = threading.Lock()
        self._original_getitem: Callable[..., Any] | None = None
        self._installed_getitem: Callable[..., Any] | None = None

    def check(self, function_name: str, arguments: tuple[Any, ...]) -> None:
        """呼出しを判定し、実入力となり得る場合は記録してInputSentinelViolationを送出する。"""
        if function_name in GLOBAL_INPUT_FUNCTIONS:
            self._block(
                SentinelEvent(function_name, None, None, None, "global-input-api")
            )
        if function_name not in WINDOW_MESSAGE_FUNCTIONS | THREAD_MESSAGE_FUNCTIONS:
            return
        if len(arguments) < 2:
            self._block(
                SentinelEvent(
                    function_name, None, None, None, "unparsable-message-call"
                )
            )
        window_handle = _plain_int(arguments[0])
        message = _plain_int(arguments[1])
        if not is_input_message(message):
            return
        if function_name in THREAD_MESSAGE_FUNCTIONS:
            self._block(
                SentinelEvent(
                    function_name, message, None, None, "thread-input-message"
                )
            )
        owner = self._owner_or_zero(window_handle)
        if owner != 0 and owner == self.current_process_id:
            with self._lock:
                self.allowed.append(
                    SentinelEvent(
                        function_name,
                        message,
                        window_handle,
                        owner,
                        "owned-test-window",
                    )
                )
            return
        self._block(
            SentinelEvent(
                function_name, message, window_handle, owner, "foreign-window-input"
            )
        )

    def _owner_or_zero(self, window_handle: int) -> int:
        """HWNDの所有PIDを返す。HWND 0や照会の例外は所有者不明（0）として遮断側に倒す。"""
        if window_handle == 0:
            return 0
        try:
            return int(self.owner_process_id_of(window_handle))
        except Exception:
            return 0

    def _block(self, event: SentinelEvent) -> None:
        if not event.context:
            event = replace(event, context=self.context)
        # 前のテストが残したworkerからの呼出しも追跡できるよう、呼出しthread名を添える。
        event = replace(event, thread=threading.current_thread().name)
        with self._lock:
            self.blocked.append(event)
        raise InputSentinelViolation(
            f"Windows platform gateが実入力の可能性があるuser32.{event.function}を遮断しました: "
            f"reason={event.reason} message={event.message} hwnd={event.window_handle} "
            f"owner_pid={event.owner_process_id} test_pid={self.current_process_id}"
        )

    def guard(self, function_name: str, function: Any) -> Any:
        """監視対象名ならGuardedFunctionで包み、それ以外はそのまま返す。"""
        # 別のInputSentinel（入れ子のテスト等）が包んだ関数は、外側として更に包んで自分でも判定する。
        if function_name not in GUARDED_FUNCTIONS or (
            isinstance(function, GuardedFunction) and function.guarded_by(self)
        ):
            return function
        return GuardedFunction(self, function_name, function)

    def install(self) -> None:
        """ctypes.CDLL.__getitem__を差し替え、以後に取得するuser32関数をすべて監視対象にする。"""
        if self._original_getitem is not None:
            raise RuntimeError("InputSentinel is already installed")
        # 監視はinstall後の取得にだけ効く。install前にimportしたモジュールが保持する関数参照は
        # 置き換えられないため、gate runnerはテスト対象をimportする前にinstallする。
        original = ctypes.CDLL.__getitem__
        sentinel = self

        def guarded_getitem(library: Any, name_or_ordinal: Any) -> Any:
            function = original(library, name_or_ordinal)
            if isinstance(name_or_ordinal, str) and is_user32_library(
                getattr(library, "_name", None)
            ):
                return sentinel.guard(name_or_ordinal, function)
            return function

        self._original_getitem = original
        self._installed_getitem = guarded_getitem
        ctypes.CDLL.__getitem__ = guarded_getitem  # type: ignore[method-assign]
        # install前にctypes.windll.user32へキャッシュ済みの関数は差し替え前のため捨てる。
        loader = getattr(ctypes, "windll", None)
        cached_user32 = vars(loader).get("user32") if loader is not None else None
        if cached_user32 is not None:
            for name in GUARDED_FUNCTIONS:
                vars(cached_user32).pop(name, None)

    def uninstall(self) -> None:
        """installで差し替えたctypes.CDLL.__getitem__を元に戻す。

        エラー: 後からinstallした別のInputSentinelが残っている（LIFO順でない）場合はRuntimeError。
        外側の監視を黙って外さないため、差し替えは行わない。
        """
        if self._original_getitem is None:
            return
        if ctypes.CDLL.__getitem__ is not self._installed_getitem:
            raise RuntimeError("InputSentinel must be uninstalled in LIFO order")
        ctypes.CDLL.__getitem__ = self._original_getitem  # type: ignore[method-assign]
        self._original_getitem = None
        self._installed_getitem = None


class GuardedFunction:
    """ctypes関数を包み、呼出し前にInputSentinel.checkを通す。argtypes等の属性は元関数へ委譲する。"""

    def __init__(
        self, sentinel: InputSentinel, function_name: str, function: Any
    ) -> None:
        object.__setattr__(self, "_sentinel", sentinel)
        object.__setattr__(self, "_function_name", function_name)
        object.__setattr__(self, "_function", function)

    def guarded_by(self, sentinel: InputSentinel) -> bool:
        """このラッパーが指定のInputSentinelで判定するならTrue。"""
        return object.__getattribute__(self, "_sentinel") is sentinel

    def __call__(self, *arguments: Any) -> Any:
        self._sentinel.check(self._function_name, arguments)
        return self._function(*arguments)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_function"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._function, name, value)


def windows_owner_process_id_of() -> Callable[[int], int]:
    """独立したWinDLL("user32")のGetWindowThreadProcessIdでHWNDの所有PIDを返す関数を作る。"""
    from ctypes import wintypes

    user32 = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
    query = user32.GetWindowThreadProcessId
    query.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.DWORD

    def owner_process_id_of(window_handle: int) -> int:
        process_id = wintypes.DWORD(0)
        if not query(window_handle, ctypes.byref(process_id)):
            return 0
        return int(process_id.value)

    return owner_process_id_of
