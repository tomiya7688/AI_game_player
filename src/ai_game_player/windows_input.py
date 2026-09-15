import ctypes
import os
import time
from typing import Callable

from ai_game_player.models import ActionCandidate


SPECIAL_KEYS = {
    "ENTER": 0x0D,
    "SPACE": 0x20,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "TAB": 0x09,
    "BACKSPACE": 0x08,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "ALT": 0x12,
    "F12": 0x7B,
}


class WindowsInputExecutor:
    """Executes already-guarded Windows input and releases any held keys on exit."""

    def __init__(
        self,
        window_handle: int | None = None,
        input_mode: str = "mouse",
        *,
        stop_checker: Callable[[], bool] | None = None,
    ) -> None:
        self.window_handle = window_handle
        self.input_mode = input_mode
        self.stop_checker = stop_checker or (lambda: False)
        self._held_keys: set[int] = set()

    def execute(self, candidate: ActionCandidate):
        from ai_game_player.action_executor import ExecutionResult

        if os.name != "nt":
            raise RuntimeError("WindowsInputExecutor requires Windows")
        if self.stop_checker():
            raise RuntimeError("emergency stop is active")
        if candidate.kind in {"click", "double_click"}:
            self._execute_click(candidate)
            detail = "Windows target message sent" if self.input_mode == "window_message" else "Windows mouse input sent"
            return ExecutionResult(candidate.action_id, True, "live", detail)
        if candidate.kind == "key":
            self._execute_key(candidate)
            return ExecutionResult(candidate.action_id, True, "live", "Windows key input sent")
        if candidate.kind == "wait":
            try:
                duration = float(candidate.label or "0.5")
            except ValueError:
                duration = 0.5
            self._interruptible_sleep(duration)
            return ExecutionResult(candidate.action_id, True, "live", "Wait completed")
        raise ValueError(f"unsupported live action kind: {candidate.kind}")

    def release_all(self) -> None:
        if os.name != "nt":
            self._held_keys.clear()
            return
        for virtual_key in tuple(self._held_keys):
            self._key_up(virtual_key)

    def _execute_click(self, candidate: ActionCandidate) -> None:
        if candidate.x is None or candidate.y is None:
            raise ValueError("click action requires coordinates")
        user32 = ctypes.windll.user32
        if self.input_mode == "window_message":
            if self.window_handle is None:
                raise RuntimeError("window_message requires a selected window")
            rect = (ctypes.c_long * 4)()
            if not user32.GetWindowRect(self.window_handle, ctypes.byref(rect)):
                raise RuntimeError("GetWindowRect failed")
            point = (ctypes.c_long * 2)(int(rect[0]) + candidate.x, int(rect[1]) + candidate.y)
            if not user32.ScreenToClient(self.window_handle, ctypes.byref(point)):
                raise RuntimeError("ScreenToClient failed")
            lparam = (int(point[1]) << 16) | (int(point[0]) & 0xFFFF)
            self._post_click(lparam)
            if candidate.kind == "double_click":
                self._interruptible_sleep(0.05)
                self._post_click(lparam)
            return

        x, y = candidate.x, candidate.y
        if self.window_handle is not None:
            rect = (ctypes.c_long * 4)()
            if not user32.GetWindowRect(self.window_handle, ctypes.byref(rect)):
                raise RuntimeError("GetWindowRect failed")
            x, y = x + int(rect[0]), y + int(rect[1])
        user32.SetCursorPos(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        if candidate.kind == "double_click":
            self._interruptible_sleep(0.05)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)

    def _post_click(self, lparam: int) -> None:
        user32 = ctypes.windll.user32
        if self.window_handle is None:
            raise RuntimeError("window_message requires a selected window")
        user32.PostMessageW(self.window_handle, 0x0201, 0x0001, lparam)
        user32.PostMessageW(self.window_handle, 0x0202, 0, lparam)

    def _execute_key(self, candidate: ActionCandidate) -> None:
        user32 = ctypes.windll.user32
        key_name = candidate.label.strip().upper()
        key_code = SPECIAL_KEYS.get(key_name, user32.VkKeyScanW(ord(candidate.label[0])) if candidate.label else -1)
        if key_code < 0:
            raise ValueError("key action requires a supported key label")
        virtual_key = key_code & 0xFF
        self._key_down(virtual_key)
        try:
            hold_seconds = float(candidate.hold_seconds or 0.0)
            if hold_seconds:
                self._interruptible_sleep(hold_seconds)
        finally:
            self._key_up(virtual_key)

    def _key_down(self, virtual_key: int) -> None:
        user32 = ctypes.windll.user32
        if self.input_mode == "window_message":
            if self.window_handle is None:
                raise RuntimeError("window_message requires a selected window")
            if not user32.PostMessageW(self.window_handle, 0x0100, virtual_key, 0):
                raise RuntimeError("PostMessageW key-down failed")
        else:
            user32.keybd_event(virtual_key, 0, 0, 0)
        self._held_keys.add(virtual_key)

    def _key_up(self, virtual_key: int) -> None:
        user32 = ctypes.windll.user32
        try:
            if self.input_mode == "window_message":
                if self.window_handle is not None:
                    user32.PostMessageW(self.window_handle, 0x0101, virtual_key, 0)
            else:
                user32.keybd_event(virtual_key, 0, 2, 0)
        finally:
            self._held_keys.discard(virtual_key)

    def _interruptible_sleep(self, duration: float) -> None:
        deadline = time.monotonic() + duration
        while True:
            if self.stop_checker():
                self.release_all()
                raise RuntimeError("emergency stop interrupted Windows input")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.02))