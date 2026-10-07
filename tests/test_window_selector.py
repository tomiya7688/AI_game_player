import os
import unittest

from ai_game_player.platform.windows.window_selector import WindowInfo, WindowsWindowSelector


class WindowSelectorTest(unittest.TestCase):
    def test_windows_with_duplicate_titles_have_distinct_display_names(self):
        first = WindowInfo(0x101, "Game")
        second = WindowInfo(0x202, "Game")

        self.assertNotEqual(first.display_name, second.display_name)
        self.assertIn("HWND 0x101", first.display_name)

    def test_non_windows_is_explicit(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError):
                WindowsWindowSelector().list_windows()
