import tkinter as tk
import unittest
import re
from unittest.mock import patch

from ai_game_player.ui.tooltip import ToolTip


class ToolTipTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        self.addCleanup(self.root.destroy)
        self.root.geometry("200x100+30000+30000")
        self.button = tk.Button(self.root, text="説明対象")
        self.button.pack()
        self.root.update()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *error: (
            self.callback_errors.append(error)
        )

    def pump_timers(self):
        self.root.after(30, self.root.quit)
        self.root.mainloop()
        self.assertEqual(self.callback_errors, [])

    def test_focus_event_shows_one_popup_and_focus_out_hides_it(self):
        tooltip = ToolTip(self.button, "実際の説明", delay_ms=1)
        self.button.event_generate("<FocusIn>")
        self.pump_timers()
        self.assertIsNone(tooltip._after_id)
        popup = tooltip._tip_window
        self.assertIsNotNone(popup)
        self.assertEqual(popup.winfo_children()[0].cget("text"), "実際の説明")
        tooltip.show()
        self.assertIs(tooltip._tip_window, popup)
        self.button.event_generate("<FocusOut>")
        self.assertIsNone(tooltip._tip_window)
        self.assertFalse(popup.winfo_exists())

    def test_leave_and_click_cancel_real_pending_timers(self):
        for exit_event in ("<Leave>", "<ButtonPress-1>"):
            with self.subTest(event=exit_event):
                button = tk.Button(self.root)
                button.pack()
                self.root.update()
                tooltip = ToolTip(button, "遅延説明", delay_ms=1)
                button.event_generate("<Enter>")
                timer = tooltip._after_id
                self.assertIsNotNone(timer)
                button.event_generate(exit_event)
                self.assertIsNone(tooltip._after_id)
                self.assertNotIn(timer, self.root.tk.call("after", "info"))
                self.pump_timers()
                self.assertIsNone(tooltip._tip_window)
                button.destroy()

    def test_destroy_target_cancels_timer_before_tk_deletes_callback(self):
        tooltip = ToolTip(self.button, "破棄後は表示しない", delay_ms=1)
        self.button.event_generate("<Enter>")
        timer = tooltip._after_id
        self.button.destroy()
        self.assertIsNone(tooltip._after_id)
        self.assertNotIn(timer, self.root.tk.call("after", "info"))
        self.pump_timers()
        self.assertIsNone(tooltip._tip_window)

    def test_unmapping_target_dismisses_visible_popup(self):
        tooltip = ToolTip(self.button, "表示解除時に終了")
        tooltip.show()
        popup = tooltip._tip_window
        self.assertIsNotNone(popup)
        self.button.pack_forget()
        self.root.update()
        self.assertIsNone(tooltip._tip_window)
        self.assertFalse(popup.winfo_exists())

    def test_display_failure_removes_partially_created_popup(self):
        tooltip = ToolTip(self.button, "表示失敗")
        with patch(
            "ai_game_player.ui.tooltip.tk.Label", side_effect=tk.TclError("label")
        ):
            tooltip.show()
        self.assertIsNone(tooltip._tip_window)
        self.assertFalse(
            any(
                isinstance(child, tk.Toplevel) for child in self.button.winfo_children()
            )
        )

    def test_popup_position_is_clamped_to_virtual_screen_edges(self):
        tooltip = ToolTip(self.button, "画面端")
        tooltip.show()
        popup = tooltip._tip_window
        self.assertIsNotNone(popup)
        self.root.update_idletasks()
        for left, top in ((0, 0), (-800, -600)):
            with (
                self.subTest(origin=(left, top)),
                patch.object(self.button, "winfo_vrootx", return_value=left),
                patch.object(self.button, "winfo_vrooty", return_value=top),
                patch.object(self.button, "winfo_vrootwidth", return_value=800),
                patch.object(self.button, "winfo_vrootheight", return_value=600),
            ):
                tooltip._position_popup(popup, left + 795, top + 595)
                self.root.update_idletasks()
                self.assertEqual(popup.winfo_x(), left + 800 - popup.winfo_reqwidth())
                self.assertEqual(popup.winfo_y(), top + 600 - popup.winfo_reqheight())
        tooltip.hide()

    def test_escape_binding_dismisses_popup_without_consuming_key(self):
        tooltip = ToolTip(self.button, "Escで閉じる")
        tooltip.show()
        self.assertIsNotNone(tooltip._tip_window)
        binding = self.button.bind("<KeyPress-Escape>")
        self.assertEqual(binding, self.button.bind("<Escape>"))
        callback = re.search(r"\[([^ ]+)", binding).group(1)
        self.assertEqual(self.root.tk.call(callback), "None")
        self.assertIsNone(tooltip._tip_window)

    def test_existing_bindings_are_preserved_and_empty_text_has_no_timer(self):
        original = self.button.bind("<Enter>", lambda _event: None)
        tooltip = ToolTip(self.button, "")
        self.assertIn(original, self.button.bind("<Enter>"))
        self.button.event_generate("<Enter>")
        self.assertIsNone(tooltip._after_id)
        tooltip.show()
        self.assertIsNone(tooltip._tip_window)
        with self.assertRaisesRegex(ValueError, "0以上"):
            ToolTip(self.button, "無効", delay_ms=-1)


if __name__ == "__main__":
    unittest.main()
