import ctypes
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player.action_executor import ActionExecutor
from ai_game_player.fail_safe_runtime import InputLedger
from ai_game_player.models import ActionCandidate
from ai_game_player.windows_input import SPECIAL_KEYS, WindowsInputExecutor


class FakeUser32:
    def __init__(self):
        self.cursor_positions = []
        self.mouse_events = []
        self.key_events = []
        self.messages = []

    def GetWindowRect(self, _handle, rect_pointer):
        rect = ctypes.cast(rect_pointer, ctypes.POINTER(ctypes.c_long * 4)).contents
        rect[:] = (100, 200, 900, 800)
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

    def keybd_event(self, virtual_key, _scan_code, flags, _extra):
        self.key_events.append((virtual_key, flags))

    def PostMessageW(self, handle, message, wparam, lparam):
        self.messages.append((handle, message, wparam, lparam))
        return 1


class WindowsInputTest(unittest.TestCase):
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
        class MemoryStore:
            def __init__(self):
                self.data = {}

            def read(self):
                return self.data.copy()

            def write(self, value):
                self.data = value.copy()

        user32 = FakeUser32()
        ledger = InputLedger(Path("unused-input-ledger.json"), store=MemoryStore(), clock=lambda: 100.0)
        executor = WindowsInputExecutor(input_ledger=ledger)

        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32), create=True):
            executor._key_down(0x41)
            executor._mouse_down("left")

            self.assertEqual(ledger.snapshot()["held_keys"], {"65": 101.0})
            self.assertEqual(ledger.snapshot()["held_mouse"], {"left": 101.0})

            executor.release_all()

        self.assertEqual(user32.key_events, [(0x41, 0), (0x41, 2)])
        self.assertEqual(user32.mouse_events, [(0x0002, 0, 0), (0x0004, 0, 0)])
        self.assertEqual(ledger.snapshot()["held_keys"], {})
        self.assertEqual(ledger.snapshot()["held_mouse"], {})

    def test_special_key_names_are_defined(self):
        self.assertEqual(SPECIAL_KEYS["ENTER"], 0x0D)
        self.assertEqual(SPECIAL_KEYS["SPACE"], 0x20)
