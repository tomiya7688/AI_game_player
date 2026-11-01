import unittest
from unittest.mock import Mock, patch

from ai_game_player.safety_guard import TargetState
from tools.planning_window_snapshot import selected_window_snapshot


class FakeUser32:
    def __init__(self, foreground=456, final_pid=7):
        self.foreground, self.final_pid = foreground, final_pid
        self.calls = []

    def GetWindowThreadProcessId(self, handle, result):
        self.calls.append(("pid", handle))
        result._obj.value = self.final_pid if handle == 123 else 8
        return 11

    def GetWindowRect(self, handle, result):
        self.calls.append(("window_rect", handle))
        result._obj[:] = [10, 20, 1290, 920]
        return True

    def GetClientRect(self, handle, result):
        self.calls.append(("client_rect", handle))
        result._obj[:] = [0, 0, 1240, 830]
        return True

    def ClientToScreen(self, handle, point):
        self.calls.append(("client_origin", handle))
        point._obj[:] = [30, 70]
        return True

    def IsWindowVisible(self, handle):
        return True

    def IsIconic(self, handle):
        return False

    def GetForegroundWindow(self):
        return self.foreground

    def GetDpiForWindow(self, handle):
        return 144


class SelectedWindowSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.target = TargetState(
            123, 7, True, True, False, 1280, 900, 20, 50, 1240, 830
        )
        self.probe = Mock(return_value=self.target)

    def test_actual_targetstate_geometry_and_foreground_are_diagnostic_only(self):
        api = FakeUser32()
        result = selected_window_snapshot(
            123, 7, frame_size=(900, 700), probe=self.probe, user32=api
        )
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["diagnostic_only"])
        self.assertEqual(result["window_rect_ltrb"], [10, 20, 1290, 920])
        self.assertEqual(result["client_screen_origin"], [30, 70])
        self.assertEqual(result["client_rect_ltrb"], [0, 0, 1240, 830])
        self.assertEqual(result["frame_size"], [900, 700])
        self.assertEqual(result["dpi"], 144)
        self.assertEqual(result["foreground_pid"], 8)
        self.assertFalse(result["foreground_is_selected"])
        self.assertFalse(result["foreground_pid_is_selected"])
        self.assertNotIn("title", result)
        self.assertNotIn("allowed", result)
        self.probe.assert_called_once_with(123)
        self.assertTrue(
            all(handle == 123 for operation, handle in api.calls if operation != "pid")
        )

    def test_unknown_owner_and_identity_change_fail_without_geometry_leak(self):
        with patch.object(
            self.probe,
            "return_value",
            TargetState(123, 8, True, True, False, 1, 1, 0, 0, 1, 1),
        ):
            api = FakeUser32()
            result = selected_window_snapshot(123, 7, probe=self.probe, user32=api)
        self.assertEqual(result["reason"], "ownership_unverified")
        self.assertEqual(api.calls, [])
        self.probe.return_value = self.target
        result = selected_window_snapshot(
            123, 7, probe=self.probe, user32=FakeUser32(final_pid=8)
        )
        self.assertEqual(result["reason"], "ownership_changed")
        self.assertNotIn("window_rect_ltrb", result)

    def test_no_foreground_is_not_misrepresented_as_owned_or_allowed(self):
        result = selected_window_snapshot(
            123, 7, probe=self.probe, user32=FakeUser32(foreground=None)
        )
        self.assertEqual(result["status"], "available")
        self.assertIsNone(result["foreground_hwnd"])
        self.assertIsNone(result["foreground_pid"])
        self.assertFalse(result["foreground_is_selected"])

    def test_bad_arguments_or_query_failure_are_unavailable(self):
        self.assertEqual(
            selected_window_snapshot(True, 7, user32=FakeUser32())["reason"],
            "invalid_expected_identity",
        )
        self.assertEqual(
            selected_window_snapshot(123, 7, frame_size=(0, 1), user32=FakeUser32())[
                "reason"
            ],
            "invalid_frame_size",
        )
        api = FakeUser32()
        api.GetWindowRect = lambda *args: False
        result = selected_window_snapshot(123, 7, probe=self.probe, user32=api)
        self.assertEqual(result["reason"], "geometry_query_failed")
        self.assertNotIn("allowed", result)


if __name__ == "__main__":
    unittest.main()
