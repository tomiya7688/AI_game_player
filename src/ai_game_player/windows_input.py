import ctypes
import os
import time

from ai_game_player.models import ActionCandidate


SPECIAL_KEYS = {"ENTER": 0x0D, "SPACE": 0x20, "ESC": 0x1B, "ESCAPE": 0x1B, "TAB": 0x09, "BACKSPACE": 0x08, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12}

class WindowsInputExecutor:
    """Sends click and key input only when explicitly selected."""

    def __init__(self, window_handle: int | None = None, input_mode: str = "mouse") -> None:
        self.window_handle = window_handle
        self.input_mode = input_mode

    def execute(self, candidate: ActionCandidate):
        from ai_game_player.action_executor import ExecutionResult
        if os.name != "nt":
            raise RuntimeError("WindowsInputExecutor requires Windows")
        user32 = ctypes.windll.user32
        if candidate.kind in {"click", "double_click"}:
            if candidate.x is None or candidate.y is None:
                raise ValueError("click action requires coordinates")
            x, y = candidate.x, candidate.y
            if self.window_handle is not None:
                rect = (ctypes.c_long * 4)()
                if not user32.GetWindowRect(self.window_handle, ctypes.byref(rect)):
                    raise RuntimeError("GetWindowRect failed")
                x, y = x + rect[0], y + rect[1]
            user32.SetCursorPos(x, y)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            if candidate.kind == "double_click":
                time.sleep(0.05)
                user32.mouse_event(0x0002, 0, 0, 0, 0)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
            return ExecutionResult(candidate.action_id, True, "live", "Windows mouse input sent")
        if candidate.kind == "key":
            key_name = candidate.label.strip().upper()
            key_code = SPECIAL_KEYS.get(key_name, user32.VkKeyScanW(ord(candidate.label[0])) if candidate.label else -1)
            if key_code < 0:
                raise ValueError("key action requires a supported key label")
            virtual_key = key_code & 0xFF
            if self.input_mode == "window_message":
                if self.window_handle is None: raise RuntimeError("window_message requires a selected window")
                user32.PostMessageW(self.window_handle, 0x0100, virtual_key, 0)
                user32.PostMessageW(self.window_handle, 0x0101, virtual_key, 0)
            else:
                user32.keybd_event(virtual_key, 0, 0, 0)
                user32.keybd_event(virtual_key, 0, 2, 0)
            return ExecutionResult(candidate.action_id, True, "live", "Windows key input sent")
        if candidate.kind == "wait":
            time.sleep(float(candidate.label or "0.5"))
            return ExecutionResult(candidate.action_id, True, "live", "Wait completed")
        raise ValueError(f"unsupported live action kind: {candidate.kind}")