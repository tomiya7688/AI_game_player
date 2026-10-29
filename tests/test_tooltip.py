import tkinter as tk
import unittest
from tkinter import TclError

from ai_game_player.ui.tooltip import ToolTip


class ToolTipTests(unittest.TestCase):
    def test_tooltip_initialization(self) -> None:
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            button = tk.Button(root, text="Test")
            tooltip = ToolTip(button, "Test Tooltip", delay_ms=200)

            self.assertEqual(tooltip.widget, button)
            self.assertEqual(tooltip.text, "Test Tooltip")
            self.assertEqual(tooltip.delay_ms, 200)
            self.assertIsNone(tooltip._tip_window)
            self.assertIsNone(tooltip._after_id)
        finally:
            root.destroy()

    def test_tooltip_show_and_hide(self) -> None:
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            button = tk.Button(root, text="Test")
            button.pack()
            root.update_idletasks()

            tooltip = ToolTip(button, "Test Tooltip", delay_ms=10)
            tooltip.show()
            self.assertIsNotNone(tooltip._tip_window)
            root.update_idletasks()
            if tooltip._tip_window is not None:
                self.assertTrue(tooltip._tip_window.winfo_viewable())

            # Calling show again when already visible does nothing
            first_window = tooltip._tip_window
            tooltip.show()
            self.assertEqual(tooltip._tip_window, first_window)

            tooltip.hide()
            self.assertIsNone(tooltip._tip_window)
        finally:
            root.destroy()

    def test_tooltip_event_bindings(self) -> None:
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            button = tk.Button(root, text="Test")
            button.pack()
            root.update_idletasks()

            tooltip = ToolTip(button, "Test Tooltip", delay_ms=10)
            tooltip._on_enter()
            self.assertIsNotNone(tooltip._after_id)

            tooltip._on_leave()
            self.assertIsNone(tooltip._after_id)
            self.assertIsNone(tooltip._tip_window)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
