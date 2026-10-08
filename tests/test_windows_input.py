import ctypes
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player import windows_input
from ai_game_player.action_executor import ActionExecutor
from ai_game_player.fail_safe_runtime import InputLedger
from ai_game_player.models import ActionCandidate
from ai_game_player.run_control import RunController
from ai_game_player.windows_input import SPECIAL_KEYS, WindowsInputExecutor


# {
#   責務: [FakeUser32: Windows APIの入力・ウィンドウ操作を記録する]
#   フィールド: [messages: ウィンドウメッセージ, mouse_events: マウス入力, key_events: キー入力]
# }
class FakeUser32:
    def __init__(self):
        self.cursor_positions = []
        self.mouse_events = []
        self.key_events = []
        self.messages = []
        self.failed_messages = set()
        self.failed_mouse_flags = set()
        self.failed_key_flags = set()

    def GetWindowRect(self, _handle, rect_pointer):
        rect = ctypes.cast(rect_pointer, ctypes.POINTER(ctypes.c_long * 4)).contents
        rect[:] = (100, 200, 900, 800)
        return 1

    # {
    #   責務: [IsWindow: fake HWNDを有効として報告する]
    #   処理: [Win32の成功値を返す]
    #   引数: [_handle: 検証対象HWND]
    #   戻り値: [int: 成功値]
    # }
    def IsWindow(self, _handle):
        return 1

    # {
    #   責務: [GetWindowThreadProcessId: fake HWNDの所有PIDを返す]
    #   処理: [期待とは異なるPIDを出力領域に設定する]
    #   引数: [_handle: 対象HWND, process_id_pointer: PID出力先]
    #   戻り値: [int: 有効なスレッドID]
    # }
    def GetWindowThreadProcessId(self, _handle, process_id_pointer):
        ctypes.cast(process_id_pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 999
        return 1

    def ScreenToClient(self, _handle, point_pointer):
        point = ctypes.cast(point_pointer, ctypes.POINTER(ctypes.c_long * 2)).contents
        point[0] -= 108
        point[1] -= 230
        return 1

    def SetCursorPos(self, x, y):
        self.cursor_positions.append((x, y))
        return 1

    def mouse_event(self, flags, _x, _y, _data, _extra):
        self.mouse_events.append((flags, _x, _y))
        if flags in self.failed_mouse_flags:
            raise RuntimeError("fake mouse event failed")

    def keybd_event(self, virtual_key, _scan_code, flags, _extra):
        self.key_events.append((virtual_key, flags))
        if flags in self.failed_key_flags:
            raise RuntimeError("fake key event failed")

    def PostMessageW(self, handle, message, wparam, lparam):
        self.messages.append((handle, message, wparam, lparam))
        return int(message not in self.failed_messages)


class MemoryLedgerStore:
    def __init__(self):
        self.data = {}

    def read(self):
        return self.data.copy()

    def write(self, value):
        self.data = value.copy()


# {
#   責務: [WindowsInputTest: Windows入力の対象検証と送信動作を検証する]
#   フィールド: [各testがfake user32とActionCandidateを使う]
# }
class WindowsInputTest(unittest.TestCase):
    def test_controller_stop_releases_key_during_live_hold(self):
        controller = RunController()
        key_pressed = threading.Event()

        class TargetUser32(FakeUser32):
            def GetWindowThreadProcessId(self, _handle, process_id_pointer):
                ctypes.cast(process_id_pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 456
                return 1

            def VkKeyScanW(self, character):
                return character

            def keybd_event(self, virtual_key, scan_code, flags, extra_info):
                super().keybd_event(virtual_key, scan_code, flags, extra_info)
                if flags == 0:
                    key_pressed.set()

        user32 = TargetUser32()
        executor = WindowsInputExecutor(
            window_handle=123,
            window_process_id=456,
            stop_checker=lambda: not controller.is_running,
        )
        candidate = ActionCandidate("hold-a", "key", "A", hold_seconds=1.0)
        execution_errors = []

        def execute_key_hold():
            try:
                executor.execute(candidate)
            except RuntimeError as exc:
                execution_errors.append(exc)

        with patch.object(windows_input.os, "name", "nt"), patch.object(
            ctypes, "windll", SimpleNamespace(user32=user32), create=True
        ):
            worker = threading.Thread(target=execute_key_hold)
            worker.start()
            self.assertTrue(key_pressed.wait(timeout=1))
            controller.stop("Stop button")
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(execution_errors), 1)
        self.assertIn("emergency stop interrupted Windows input", str(execution_errors[0]))
        self.assertIn((0x41, 2), user32.key_events)

    def test_unsupported_input_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported Windows input mode"):
            WindowsInputExecutor(input_mode="unknown")

    def test_window_message_click_without_target_never_sends_global_mouse_input(self):
        user32 = FakeUser32()
        executor = WindowsInputExecutor(input_mode="window_message")
        candidate = ActionCandidate("click", "click", "Play", 30, 45)

        with patch.object(windows_input.os, "name", "nt"), patch.object(
            ctypes, "windll", SimpleNamespace(user32=user32), create=True
        ):
            with self.assertRaisesRegex(RuntimeError, "requires a selected window"):
                executor.execute(candidate)

        self.assertEqual(user32.messages, [])
        self.assertEqual(user32.cursor_positions, [])
        self.assertEqual(user32.mouse_events, [])

    def test_recycled_handle_is_rejected_before_sending_input(self):
        # {
        #   責務: [test_recycled_handle_is_rejected_before_sending_input: HWND所有PID不一致で入力を拒否する]
        #   処理: [異なるPIDを返すfake user32に対するクリックを実行し拒否を検証する]
        #   引数: []
        #   戻り値: []
        # }
        user32 = FakeUser32()
        executor = WindowsInputExecutor(window_handle=123, input_mode="window_message", window_process_id=456)
        candidate = ActionCandidate("click", "click", "Play", 30, 45)

        with patch.object(windows_input.os, "name", "nt"), patch.object(
            ctypes, "windll", SimpleNamespace(user32=user32), create=True
        ):
            with self.assertRaisesRegex(RuntimeError, "different process"):
                executor.execute(candidate)

        self.assertEqual(user32.messages, [])

    def test_live_executor_is_explicit_on_non_windows(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError):
                ActionExecutor(False).execute(ActionCandidate("a", "click", "A", 1, 1))

    def test_custom_live_executor_can_be_verified_without_os_input(self):
        class Fake:
            def execute(self, candidate):
                return type("Result", (), {"action_id": candidate.action_id, "executed": True, "mode": "fake", "detail": "ok"})()
        result = ActionExecutor(False, Fake()).execute(ActionCandidate("a", "click", "A", 1, 1))
        self.assertTrue(result.executed)
        self.assertEqual(result.mode, "fake")

    def test_mouse_input_translates_window_coordinates_to_screen_without_live_input(self):
        user32 = FakeUser32()
        executor = WindowsInputExecutor(window_handle=123, input_mode="mouse")
        candidate = ActionCandidate("click", "click", "Play", 30, 45)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            executor._execute_click(candidate)

        self.assertEqual(user32.cursor_positions, [(130, 245)])
        self.assertEqual(user32.mouse_events, [(0x0002, 0, 0), (0x0004, 0, 0)])

    def test_window_message_translates_window_coordinates_to_client_lparam(self):
        user32 = FakeUser32()
        executor = WindowsInputExecutor(window_handle=123, input_mode="window_message")
        candidate = ActionCandidate("click", "click", "Play", 30, 45)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            executor._execute_click(candidate)

        expected_lparam = (15 << 16) | 22
        expected_messages = [(123, 0x0201, 0x0001, expected_lparam), (123, 0x0202, 0, expected_lparam)]
        self.assertEqual(user32.messages, expected_messages)
        self.assertEqual(user32.cursor_positions, [])

    def test_release_all_releases_fake_held_inputs_and_clears_ledger(self):
        user32 = FakeUser32()
        ledger = InputLedger(Path("unused-input-ledger.json"), store=MemoryLedgerStore(), clock=lambda: 100.0)
        executor = WindowsInputExecutor(input_ledger=ledger)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            executor._key_down(0x41)
            executor._mouse_down("left")

            self.assertEqual(ledger.snapshot()["held_keys"], {"65": 101.0})
            self.assertEqual(ledger.snapshot()["held_mouse"], {"left": 101.0})

            with patch.object(windows_input.os, "name", "nt"):
                executor.release_all()

        self.assertEqual(user32.key_events, [(0x41, 0), (0x41, 2)])
        self.assertEqual(user32.mouse_events, [(0x0002, 0, 0), (0x0004, 0, 0)])
        self.assertEqual(ledger.snapshot()["held_keys"], {})
        self.assertEqual(ledger.snapshot()["held_mouse"], {})

    def test_failed_window_messages_leave_no_held_state_or_ledger_entry(self):
        user32 = FakeUser32()
        ledger = InputLedger(Path("unused-input-ledger.json"), store=MemoryLedgerStore(), clock=lambda: 100.0)
        executor = WindowsInputExecutor(window_handle=123, input_mode="window_message", input_ledger=ledger)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            user32.failed_messages = {0x0100}
            with self.assertRaisesRegex(RuntimeError, "key-down failed"):
                executor._key_down(0x41)

            self.assertEqual(ledger.snapshot()["held_keys"], {})
            self.assertEqual(executor._held_keys, set())

            user32.failed_messages = {0x0201}
            with self.assertRaisesRegex(RuntimeError, "mouse-down failed"):
                executor._post_click(0)

        self.assertEqual(ledger.snapshot()["held_mouse"], {})
        self.assertEqual(executor._held_mouse, set())
        self.assertEqual(user32.messages, [(123, 0x0100, 0x41, 0), (123, 0x0201, 0x0001, 0)])
        self.assertEqual(user32.key_events, [])
        self.assertEqual(user32.mouse_events, [])

    def test_release_all_retries_failed_window_message_mouse_up_without_os_input(self):
        user32 = FakeUser32()
        ledger = InputLedger(Path("unused-input-ledger.json"), store=MemoryLedgerStore(), clock=lambda: 100.0)
        executor = WindowsInputExecutor(window_handle=123, input_mode="window_message", input_ledger=ledger)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            user32.failed_messages = {0x0202}
            with self.assertRaisesRegex(RuntimeError, "mouse-up failed"):
                executor._post_click(0x1234)

            self.assertEqual(executor._held_mouse, {"left"})
            self.assertEqual(ledger.snapshot()["held_mouse"], {"left": 101.0})

            user32.failed_messages.clear()
            with patch.object(windows_input.os, "name", "nt"):
                executor.release_all()

        self.assertEqual(user32.messages, [(123, 0x0201, 0x0001, 0x1234), (123, 0x0202, 0, 0x1234), (123, 0x0202, 0, 0x1234)])
        self.assertEqual(user32.mouse_events, [])
        self.assertEqual(ledger.snapshot()["held_mouse"], {})

    def test_release_all_retries_failed_window_message_key_up(self):
        user32 = FakeUser32()
        ledger = InputLedger(Path("unused-input-ledger.json"), store=MemoryLedgerStore(), clock=lambda: 100.0)
        executor = WindowsInputExecutor(window_handle=123, input_mode="window_message", input_ledger=ledger)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            executor._key_down(0x41)
            user32.failed_messages = {0x0101}
            with patch.object(windows_input.os, "name", "nt"):
                with self.assertRaisesRegex(RuntimeError, "key-up failed"):
                    executor.release_all()

            self.assertEqual(executor._held_keys, {0x41})
            self.assertEqual(ledger.snapshot()["held_keys"], {"65": 101.0})

            user32.failed_messages.clear()
            with patch.object(windows_input.os, "name", "nt"):
                executor.release_all()

        self.assertEqual(user32.messages, [(123, 0x0100, 0x41, 0), (123, 0x0101, 0x41, 0), (123, 0x0101, 0x41, 0)])
        self.assertEqual(user32.key_events, [])
        self.assertEqual(ledger.snapshot()["held_keys"], {})

    def test_release_all_keeps_global_key_ledger_after_release_api_failure(self):
        user32 = FakeUser32()
        ledger = InputLedger(Path("unused-input-ledger.json"), store=MemoryLedgerStore(), clock=lambda: 100.0)
        executor = WindowsInputExecutor(input_ledger=ledger)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            executor._key_down(0x41)
            user32.failed_key_flags = {2}
            with patch.object(windows_input.os, "name", "nt"):
                with self.assertRaisesRegex(RuntimeError, "fake key event failed"):
                    executor.release_all()

                self.assertEqual(executor._held_keys, {0x41})
                self.assertEqual(ledger.snapshot()["held_keys"], {"65": 101.0})

                user32.failed_key_flags.clear()
                executor.release_all()

        self.assertEqual(user32.key_events, [(0x41, 0), (0x41, 2), (0x41, 2)])
        self.assertEqual(ledger.snapshot()["held_keys"], {})

    def test_release_all_keeps_global_mouse_ledger_after_release_api_failure(self):
        user32 = FakeUser32()
        ledger = InputLedger(Path("unused-input-ledger.json"), store=MemoryLedgerStore(), clock=lambda: 100.0)
        executor = WindowsInputExecutor(input_ledger=ledger)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            executor._mouse_down("left")
            user32.failed_mouse_flags = {0x0004}
            with patch.object(windows_input.os, "name", "nt"):
                with self.assertRaisesRegex(RuntimeError, "fake mouse event failed"):
                    executor.release_all()

                self.assertEqual(executor._held_mouse, {"left"})
                self.assertEqual(ledger.snapshot()["held_mouse"], {"left": 101.0})

                user32.failed_mouse_flags.clear()
                executor.release_all()

        self.assertEqual(user32.mouse_events, [(0x0002, 0, 0), (0x0004, 0, 0), (0x0004, 0, 0)])
        self.assertEqual(ledger.snapshot()["held_mouse"], {})

    def test_special_key_names_are_defined(self):
        self.assertEqual(SPECIAL_KEYS["ENTER"], 0x0D)
        self.assertEqual(SPECIAL_KEYS["SPACE"], 0x20)
