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


# {
#   責務: [
#     WindowsInputExecutor: HWND・PIDが一致した対象へWindows入力を送る
#   ]
#   フィールド: [
#     window_handle: 選択済みウィンドウハンドル
#     window_process_id: ウィンドウ選択時の所有PID
#     input_mode: window_messageまたはグローバルmouse方式
#   ]
#   処理: [
#     1: 入力対象の現行PIDを実行直前に検証する
#     2: 選択された入力方式に限定して操作する
#     3: held状態をfail-safe ledgerと同期する
#   ]
# }
class WindowsInputExecutor:
    """Executes already-guarded Windows input and mirrors held state to the fail-safe ledger."""

    # {
    #   責務: [
    #     __init__: 対象ウィンドウの識別情報と停止・ledger設定を保持する
    #   ]
    #   処理: [
    #     1: HWND・入力方式・期待PIDを保存する
    #     2: stop checkerとledgerを設定する
    #     3: 不正な入力方式とhold TTLを拒否する
    #   ]
    #   引数: [
    #     window_handle: 選択対象のHWND
    #     input_mode: 入力方式
    #     window_process_id: 選択時の対象プロセスID
    #     stop_checker: 緊急停止状態を確認する関数
    #     input_ledger: held入力状態の記録先
    #   ]
    #   戻り値: []
    #   エラー: [
    #     未対応入力方式または非正数hold TTLでValueError
    #   ]
    # }
    def __init__(
        self,
        window_handle: int | None = None,
        input_mode: str = "mouse",
        window_process_id: int | None = None,
        *,
        stop_checker: Callable[[], bool] | None = None,
        input_ledger: InputLedger | None = None,
        hold_ttl_seconds: float | None = None,
    ) -> None:
        self.window_handle = window_handle
        if input_mode not in {"mouse", "window_message"}:
            raise ValueError(f"unsupported Windows input mode: {input_mode}")
        self.input_mode = input_mode
        self.window_process_id = window_process_id
        self.stop_checker = stop_checker or (lambda: False)
        self.input_ledger = input_ledger
        self.hold_ttl_seconds = 1.0 if hold_ttl_seconds is None else float(hold_ttl_seconds)
        if self.hold_ttl_seconds <= 0:
            raise ValueError("hold TTL must be positive")
        self._held_keys: set[int] = set()
        self._held_mouse: set[str] = set()
        self._held_mouse_lparams: dict[str, int] = {}

    # {
    #   責務: [
    #     execute: 停止・対象同一性を確認して候補操作を実行する
    #   ]
    #   処理: [
    #     1: Windows実行環境と停止状態を確認する
    #     2: 入力操作ならHWNDとPIDが選択時のままか確認する
    #     3: click/key/wait候補を対応する経路で処理する
    #   ]
    #   引数: [
    #     candidate: 安全評価済みの操作候補
    #   ]
    #   戻り値: [
    #     ExecutionResult: 実行結果
    #   ]
    #   エラー: [
    #     Windows以外・停止中・対象不一致・未対応候補でRuntimeErrorまたはValueError
    #   ]
    # }
    def execute(self, candidate: ActionCandidate) -> ExecutionResult:
        from ai_game_player.action_executor import ExecutionResult

        if os.name != "nt":
            raise RuntimeError("WindowsInputExecutor requires Windows")
        if self.stop_checker():
            raise RuntimeError("emergency stop is active")
        if candidate.kind in {"click", "double_click", "key"}:
            self._verify_target_identity()
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

    # {
    #   責務: [
    #     _verify_target_identity: 入力直前にHWNDが列挙時のPIDの所有物か検証する
    #   ]
    #   処理: [
    #     1: HWNDと期待PIDの存在を確認する
    #     2: IsWindowでハンドルの有効性を確認する
    #     3: GetWindowThreadProcessIdの結果を期待PIDと比較する
    #   ]
    #   引数: []
    #   戻り値: []
    #   エラー: [
    #     対象未選択・失効・PID変更でRuntimeError
    #   ]
    # }
    def _verify_target_identity(self) -> None:
        if self.window_handle is None or not self.window_process_id:
            raise RuntimeError("live Windows input requires a selected window and its process identity")

        user32 = getattr(ctypes, "windll").user32
        if not user32.IsWindow(self.window_handle):
            raise RuntimeError("selected target window is no longer available")

        process_id = ctypes.c_ulong()
        if not user32.GetWindowThreadProcessId(self.window_handle, ctypes.byref(process_id)):
            raise RuntimeError("selected target process identity is unavailable")
        if int(process_id.value) != self.window_process_id:
            raise RuntimeError("selected target window now belongs to a different process")

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
        user32 = getattr(ctypes, "windll").user32
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
        user32 = getattr(ctypes, "windll").user32
        if self.window_handle is None:
            raise RuntimeError("window_message requires a selected window")
        self._ledger_hold_mouse("left")
        down_sent = False
        up_sent = False
        try:
            if not user32.PostMessageW(self.window_handle, 0x0201, 0x0001, lparam):
                raise RuntimeError("PostMessageW mouse-down failed")
            down_sent = True
            self._held_mouse.add("left")
            self._held_mouse_lparams["left"] = lparam
            if not user32.PostMessageW(self.window_handle, 0x0202, 0, lparam):
                raise RuntimeError("PostMessageW mouse-up failed")
            up_sent = True
        finally:
            if not down_sent or up_sent:
                self._held_mouse.discard("left")
                self._held_mouse_lparams.pop("left", None)
                self._ledger_release_mouse("left")

    def _execute_key(self, candidate: ActionCandidate) -> None:
        user32 = getattr(ctypes, "windll").user32
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
        user32 = getattr(ctypes, "windll").user32
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
        user32 = getattr(ctypes, "windll").user32
        released = False
        try:
            if self.input_mode == "window_message":
                if self.window_handle is None:
                    raise RuntimeError("window_message requires a selected window")
                if not user32.PostMessageW(self.window_handle, 0x0101, virtual_key, 0):
                    raise RuntimeError("PostMessageW key-up failed")
            else:
                user32.keybd_event(virtual_key, 0, 2, 0)
            released = True
        finally:
            if released:
                self._held_keys.discard(virtual_key)
                self._ledger_release_key(virtual_key)

    def _mouse_down(self, button: str) -> None:
        if button != "left":
            raise ValueError(f"unsupported mouse button: {button}")
        self._ledger_hold_mouse(button)
        try:
            getattr(ctypes, "windll").user32.mouse_event(0x0002, 0, 0, 0, 0)
        except Exception:
            self._ledger_release_mouse(button)
            raise
        self._held_mouse.add(button)

    def _mouse_up(self, button: str) -> None:
        released = False
        try:
            if self.input_mode == "window_message":
                if self.window_handle is None:
                    raise RuntimeError("window_message requires a selected window")
                lparam = self._held_mouse_lparams.get(button, 0)
                if not getattr(ctypes, "windll").user32.PostMessageW(self.window_handle, 0x0202, 0, lparam):
                    raise RuntimeError("PostMessageW mouse-up failed")
            elif button == "left":
                getattr(ctypes, "windll").user32.mouse_event(0x0004, 0, 0, 0, 0)
            released = True
        finally:
            if released:
                self._held_mouse.discard(button)
                self._held_mouse_lparams.pop(button, None)
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
