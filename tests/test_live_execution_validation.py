import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player import app as app_module
from ai_game_player.app import Application


class FakeVariable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class FakeUser32:
    def __init__(self, valid_handles):
        self.valid_handles = set(valid_handles)
        self.checked_handles = []

    def IsWindow(self, handle):
        self.checked_handles.append(handle)
        return int(handle in self.valid_handles)


class RecordingLog:
    def __init__(self):
        self.records = []

    def write(self, *record):
        self.records.append(record)


class LiveExecutionValidationTests(unittest.TestCase):
    def make_application(self, *, live=True, mode="window_message", handles=None):
        application = Application.__new__(Application)
        application.live_execution = FakeVariable(live)
        application.input_mode = FakeVariable(mode)
        application.window_choice = FakeVariable("Game")
        application.window_handles = handles or {}
        return application

    def test_live_execution_requires_a_target_in_every_input_mode(self):
        for mode in ("mouse", "window_message"):
            with self.subTest(mode=mode):
                application = self.make_application(mode=mode)
                with self.assertRaisesRegex(ValueError, "対象ウィンドウの選択"):
                    application._validate_live_execution()

    def test_dry_run_does_not_require_a_live_target(self):
        application = self.make_application(live=False)
        application._validate_live_execution()

    def test_lost_window_handle_is_rejected(self):
        application = self.make_application(handles={"Game": 123})
        user32 = FakeUser32(valid_handles=[])

        with patch.object(app_module.os, "name", "nt"), patch.object(
            ctypes, "windll", SimpleNamespace(user32=user32), create=True
        ):
            with self.assertRaisesRegex(ValueError, "ウィンドウは利用できません"):
                application._validate_live_execution()

        self.assertEqual(user32.checked_handles, [123])

    def test_run_and_execute_validates_before_persisting_or_constructing_pipeline(self):
        application = self.make_application()
        application.config_store = SimpleNamespace(save=lambda _config: self.fail("config saved before validation"))
        application.runtime_log = RecordingLog()

        with patch.object(app_module.messagebox, "showerror") as show_error:
            application.run_and_execute()

        show_error.assert_called_once()
        self.assertIn("対象ウィンドウの選択", show_error.call_args.args[1])
        self.assertEqual(application.runtime_log.records[0][0], "error")


if __name__ == "__main__":
    unittest.main()
