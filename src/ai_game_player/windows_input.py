from __future__ import annotations

import ctypes
import os
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ai_game_player.action_executor import ExecutionResult

from ai_game_player.fail_safe_runtime import InputLedger
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
    """Executes already-guarded Windows input and mirrors held state to the fail-safe ledger."""

    def __init__(
        self,
        window_handle: int | None = None,
        input_mode: str = "mouse",
        *,
        stop_checker: Callable[[], bool] | None = None,
        input_ledger: InputLedger | None = None,
        hold_ttl_seconds: float | None = None,
    ) -> None:
        self.window_handle = window_handle
        self.input_mode = input_mode
        self.stop_checker = stop_checker or (lambda: False)
        self.input_ledger = input_ledger
        self.hold_ttl_seconds = 1.0 if hold_ttl_seconds is None else float(hold_ttl_seconds)
        if self.hold_ttl_seconds <= 0:
            raise ValueError("hold TTL must be positive")
        self._held_keys: set[int] = set()
        self._held_mouse: set[str] = set()

    def execute(self, candidate: ActionCandidate) -> ExecutionResult:
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
            self._held_mouse.clear()
            if self.input_ledger is not None:
                try:
                    self.input_ledger.clear()
                except Exception:
                    pass
            return
        for virtual_key in tuple(self._held_keys):
            self._key_up(virtual_key)
        for button in tuple(self._held_mouse):
            self._mouse_up(button)

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
        self._mouse_down("left")
        try:
            self._mouse_up("left")
        except Exception:
            self._mouse_up("left")
            raise
        if candidate.kind == "double_click":
            self._interruptible_sleep(0.05)
            self._mouse_down("left")
            try:
                self._mouse_up("left")
            except Exception:
                self._mouse_up("left")
                raise

    def _post_click(self, lparam: int) -> None:
        user32 = ctypes.windll.user32
        if self.window_handle is None:
            raise RuntimeError("window_message requires a selected window")
        self._ledger_hold_mouse("left")
        self._held_mouse.add("left")
        try:
            if not user32.PostMessageW(self.window_handle, 0x0201, 0x0001, lparam):
                raise RuntimeError("PostMessageW mouse-down failed")
            if not user32.PostMessageW(self.window_handle, 0x0202, 0, lparam):
                raise RuntimeError("PostMessageW mouse-up failed")
        finally:
            self._held_mouse.discard("left")
            self._ledger_release_mouse("left")

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
        self._ledger_hold_key(virtual_key)
        try:
            if self.input_mode == "window_message":
                if self.window_handle is None:
                    raise RuntimeError("window_message requires a selected window")
                if not user32.PostMessageW(self.window_handle, 0x0100, virtual_key, 0):
                    raise RuntimeError("PostMessageW key-down failed")
            else:
                user32.keybd_event(virtual_key, 0, 0, 0)
        except Exception:
            self._ledger_release_key(virtual_key)
            raise
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
            self._ledger_release_key(virtual_key)

    def _mouse_down(self, button: str) -> None:
        if button != "left":
            raise ValueError(f"unsupported mouse button: {button}")
        self._ledger_hold_mouse(button)
        try:
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        except Exception:
            self._ledger_release_mouse(button)
            raise
        self._held_mouse.add(button)

    def _mouse_up(self, button: str) -> None:
        try:
            if button == "left":
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        finally:
            self._held_mouse.discard(button)
            self._ledger_release_mouse(button)

    def _ledger_hold_key(self, virtual_key: int) -> None:
        if self.input_ledger is not None:
            self.input_ledger.hold_key(virtual_key, self.hold_ttl_seconds)

    def _ledger_release_key(self, virtual_key: int) -> None:
        if self.input_ledger is not None:
            try:
                self.input_ledger.release_key(virtual_key)
            except Exception:
                pass

    def _ledger_hold_mouse(self, button: str) -> None:
        if self.input_ledger is not None:
            self.input_ledger.hold_mouse(button, self.hold_ttl_seconds)

    def _ledger_release_mouse(self, button: str) -> None:
        if self.input_ledger is not None:
            try:
                self.input_ledger.release_mouse(button)
            except Exception:
                pass

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
