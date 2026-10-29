import tkinter as tk
import unittest

from ai_game_player.ui.stop_help_hint import StopHelpHint


class StopHelpHintTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        self.addCleanup(self.root.destroy)
        self.root.geometry("600x260+30000+30000")
        self.hint = StopHelpHint(self.root)
        self.hint.pack(fill=tk.X)
        self.root.update()

    def test_hint_explains_common_methods_without_promising_extra_methods(self):
        self.assertTrue(self.hint.winfo_viewable())
        text = self.hint.cget("text")
        self.assertIn("停止ボタン / 本アプリにフォーカスがある状態のEsc", text)
        self.assertIn("マウスを置くかTabでフォーカス", text)
        self.assertNotIn("追加手段", text)
        self.assertNotIn("F12", text)
        self.assertEqual(str(self.hint.cget("justify")), tk.LEFT)

    def test_text_reflows_when_available_width_decreases(self):
        wide_height = self.hint.winfo_reqheight()
        self.assertEqual(
            str(self.hint.cget("wraplength")), str(self.hint.winfo_width())
        )
        self.root.geometry("240x260+30000+30000")
        self.root.update()
        self.assertEqual(self.hint.winfo_width(), self.root.winfo_width())
        self.assertEqual(
            str(self.hint.cget("wraplength")), str(self.hint.winfo_width())
        )
        self.assertGreater(self.hint.winfo_reqheight(), wide_height)


if __name__ == "__main__":
    unittest.main()
