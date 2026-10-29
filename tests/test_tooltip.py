import tkinter as tk
import unittest
from tkinter import TclError

from ai_game_player.ui.tooltip import ToolTip


class ToolTipTests(unittest.TestCase):
    def test_tooltip_initialization_and_events(self):
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            button = tk.Button(root, text="Test")
            button.pack()
            tooltip = ToolTip(button, "Helpful tip", delay_ms=10)

            self.assertEqual(tooltip.text, "Helpful tip")
            self.assertIsNone(tooltip._after_id)
            self.assertIsNone(tooltip._tip_window)

            # Schedule tip
            tooltip._schedule_tip()
            self.assertIsNotNone(tooltip._after_id)

            # Execute scheduled tip
            tooltip._show_tip()
            self.assertIsNotNone(tooltip._tip_window)

            # Hide tip
            tooltip._hide_tip()
            self.assertIsNone(tooltip._tip_window)
            self.assertIsNone(tooltip._after_id)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
