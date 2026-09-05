import ctypes
import os
import time

from ai_game_player.models import ActionCandidate


class WindowsInputExecutor:
    """Sends click and key input only when explicitly selected."""

    def execute(self, candidate: ActionCandidate):
        from ai_game_player.action_executor import ExecutionResult
        if os.name != "nt":
            raise RuntimeError("WindowsInputExecutor requires Windows")
        user32 = ctypes.windll.user32
        if candidate.kind in {"click", "double_click"}:
            if candidate.x is None or candidate.y is None:
                raise ValueError("click action requires coordinates")
            user32.SetCursorPos(candidate.x, candidate.y)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            if candidate.kind == "double_click":
                time.sleep(0.05)
                user32.mouse_event(0x0002, 0, 0, 0, 0)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
            return ExecutionResult(candidate.action_id, True, "live", "Windows mouse input sent")
        if candidate.kind == "key":
            key_code = user32.VkKeyScanW(ord(candidate.label[0])) if candidate.label else -1
            if key_code < 0:
                raise ValueError("key action requires a supported key label")
            user32.keybd_event(key_code & 0xFF, 0, 0, 0)
            user32.keybd_event(key_code & 0xFF, 0, 2, 0)
            return ExecutionResult(candidate.action_id, True, "live", "Windows key input sent")
        if candidate.kind == "wait":
            time.sleep(float(candidate.label or "0.5"))
            return ExecutionResult(candidate.action_id, True, "live", "Wait completed")
        raise ValueError(f"unsupported live action kind: {candidate.kind}")