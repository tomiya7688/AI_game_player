from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_game_player.window_selector import WindowInfo
from tools import reference_game_2048_launcher as launcher_module
from tools.reference_game_2048_launcher import BrowserWindowSession, ReferenceGame2048Launcher


class BrowserProfileCleanupTests(unittest.TestCase):
    def test_close_waits_for_transient_profile_lock_then_clears_state(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        profile = Mock()
        profile.cleanup.side_effect = [PermissionError("locked"), PermissionError("locked"), None]
        session._profile = profile
        session.window_info = WindowInfo(1, "2048", 2)
        with patch.object(launcher_module.time, "sleep") as sleep:
            session.close()
        self.assertEqual(profile.cleanup.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIsNone(session._profile)
        self.assertIsNone(session.window_info)

    def test_permanent_lock_is_bounded_and_keeps_profile_for_explicit_retry(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        profile = Mock()
        profile.cleanup.side_effect = PermissionError("locked")
        session._profile = profile
        session.window_info = WindowInfo(1, "2048", 2)
        with patch.object(launcher_module.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                session.close()
        self.assertEqual(profile.cleanup.call_count, launcher_module.PROFILE_CLEANUP_MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, launcher_module.PROFILE_CLEANUP_MAX_ATTEMPTS - 1)
        self.assertIs(session._profile, profile)
        self.assertIsNone(session.window_info)
        profile.cleanup.side_effect = None
        session.close()
        self.assertIsNone(session._profile)

    def test_non_permission_cleanup_error_is_not_retried(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        profile = Mock()
        profile.cleanup.side_effect = OSError("disk failure")
        session._profile = profile
        with patch.object(launcher_module.time, "sleep") as sleep:
            with self.assertRaisesRegex(OSError, "disk failure"):
                session.close()
        profile.cleanup.assert_called_once()
        sleep.assert_not_called()

    def test_startup_timeout_is_not_replaced_by_cleanup_error(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        startup_error = TimeoutError("owned window not found")
        cleanup_error = PermissionError("profile locked")
        with (
            patch.object(launcher_module, "os", SimpleNamespace(name="nt")),
            patch.object(Path, "is_file", return_value=True),
            patch.object(launcher_module.tempfile, "TemporaryDirectory", return_value=Mock(name="profile")),
            patch.object(launcher_module.subprocess, "Popen", return_value=Mock(pid=7)),
            patch.object(session, "_wait_for_window", side_effect=startup_error),
            patch.object(session, "close", side_effect=cleanup_error),
        ):
            with self.assertRaises(TimeoutError) as raised:
                session.start("http://127.0.0.1:8000/")
        self.assertIs(raised.exception, startup_error)
        self.assertIs(raised.exception.__context__, cleanup_error)
        self.assertIsNone(raised.exception.__cause__)

    def test_launcher_preserves_startup_error_and_failed_cleanup_owner(self):
        launcher = ReferenceGame2048Launcher(Path("cache"), Path("browser.exe"))
        browser = Mock()
        startup_error = TimeoutError("owned window not found")
        cleanup_error = PermissionError("profile locked")
        browser.start.side_effect = startup_error
        browser.close.side_effect = cleanup_error
        launcher.server = Mock()
        with (
            patch.object(launcher_module, "os", SimpleNamespace(name="nt")),
            patch.object(launcher_module, "PinnedGameSource") as source,
            patch.object(launcher_module, "BrowserWindowSession", return_value=browser),
        ):
            source.return_value.materialize.return_value = Path("source")
            with self.assertRaises(TimeoutError) as raised:
                launcher.start()
        self.assertIs(raised.exception, startup_error)
        self.assertIs(raised.exception.__context__, cleanup_error)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIs(launcher.browser, browser)
        launcher.server.close.assert_called_once()
        browser.close.side_effect = None
        launcher.close()
        self.assertIsNone(launcher.browser)


class BrowserStartupDiagnosticTests(unittest.TestCase):
    def test_timeout_reports_counts_without_leaking_unrelated_window_titles(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        session.process = SimpleNamespace(pid=42, poll=lambda: None)
        windows = [
            WindowInfo(1, "New tab", 42), WindowInfo(2, "2048 private-other-title", 99),
            WindowInfo(3, "", 42), WindowInfo(4, "", 99),
        ]
        with (
            patch("ai_game_player.window_selector.WindowsWindowSelector") as selector,
            patch.object(launcher_module.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
            patch.object(launcher_module.time, "sleep"),
        ):
            selector.return_value.list_windows.return_value = windows
            with self.assertRaises(TimeoutError) as raised:
                session._wait_for_window(0.5)
        message = str(raised.exception)
        self.assertIn("launched_pid=42", message)
        self.assertIn("owned_titled_windows=1", message)
        self.assertIn("other_2048_windows=1", message)
        self.assertNotIn("private-other-title", message)
        self.assertNotIn("New tab", message)

    def test_only_matching_pid_and_game_title_can_be_selected(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        session.process = SimpleNamespace(pid=42, poll=lambda: None)
        owned_game = WindowInfo(3, "2048", 42)
        with (
            patch("ai_game_player.window_selector.WindowsWindowSelector") as selector,
            patch.object(launcher_module.time, "monotonic", side_effect=[0.0, 0.0]),
        ):
            selector.return_value.list_windows.return_value = [
                WindowInfo(1, "2048", 99), WindowInfo(2, "New tab", 42), owned_game,
            ]
            self.assertIs(session._wait_for_window(0.5), owned_game)

    def test_process_exit_reports_the_launched_pid_and_exit_code(self):
        session = BrowserWindowSession(Path("browser.exe"), Path("source"))
        session.process = SimpleNamespace(pid=42, returncode=7, poll=lambda: 7)
        with (
            patch("ai_game_player.window_selector.WindowsWindowSelector"),
            patch.object(launcher_module.time, "monotonic", side_effect=[0.0, 0.0]),
        ):
            with self.assertRaisesRegex(RuntimeError, "launched_pid=42.*exit_code=7"):
                session._wait_for_window(0.5)


if __name__ == "__main__":
    unittest.main()
