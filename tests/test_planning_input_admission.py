import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_game_player import windows_input
from ai_game_player.models import ActionCandidate
from ai_game_player.windows_input import WindowsInputExecutor
from test_windows_input import FakeUser32


class PlanningInputAdmissionTests(unittest.TestCase):
    def test_expiry_after_ledger_preparation_never_calls_os_api(self):
        for mode in ("mouse", "window_message"):
            with self.subTest(mode=mode):
                api = FakeUser32()
                expired = [False]
                releases = []

                def admission():
                    if expired[0]:
                        raise RuntimeError("plan expired during preparation")

                executor = WindowsInputExecutor(
                    123, mode, 456, input_admission=admission
                )
                with (
                    patch.object(windows_input.os, "name", "nt"),
                    patch.object(
                        ctypes, "windll", SimpleNamespace(user32=api), create=True
                    ),
                    patch.object(executor, "_verify_target_identity"),
                    patch.object(
                        executor,
                        "_ledger_hold_key",
                        side_effect=lambda key: expired.__setitem__(0, True),
                    ),
                    patch.object(
                        executor, "_ledger_release_key", side_effect=releases.append
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "expired"):
                        executor.execute(ActionCandidate("move", "key", "LEFT"))
                self.assertFalse(executor.input_dispatch_started)
                self.assertEqual(api.messages, [])
                self.assertEqual(api.key_events, [])
                self.assertEqual(releases, [0x25])
                self.assertFalse(executor.has_held_inputs)

    def test_expiry_after_down_does_not_block_owned_key_release(self):
        api = FakeUser32()
        calls = []

        def admission():
            calls.append("admit")
            if len(calls) > 1:
                raise RuntimeError("expired")

        executor = WindowsInputExecutor(123, "mouse", 456, input_admission=admission)
        with (
            patch.object(windows_input.os, "name", "nt"),
            patch.object(ctypes, "windll", SimpleNamespace(user32=api), create=True),
            patch.object(executor, "_verify_target_identity"),
        ):
            executor.execute(ActionCandidate("move", "key", "LEFT"))
        self.assertEqual(calls, ["admit"])
        self.assertEqual(len(api.key_events), 2)
        self.assertFalse(executor.has_held_inputs)
