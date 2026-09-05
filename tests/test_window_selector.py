import os
import unittest

from ai_game_player.window_selector import WindowsWindowSelector


class WindowSelectorTest(unittest.TestCase):
    def test_non_windows_is_explicit(self):
        if os.name != "nt":
            with self.assertRaises(RuntimeError):
                WindowsWindowSelector().list_windows()