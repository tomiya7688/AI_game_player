import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player.app import Application
from ai_game_player.config import AppConfig
from ai_game_player.ui.shell import ShellState
from ai_game_player.ui.tooltip import ToolTip


class LiveInputToolTipTests(unittest.TestCase):
    def test_live_input_guidance_shows_without_creating_an_input_runtime(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk desktop is unavailable: {error}")
        self.addCleanup(root.destroy)
        tooltips = []

        def attach_tooltip(widget, text):
            tooltip = ToolTip(widget, text, delay_ms=1)
            tooltips.append(tooltip)
            return tooltip

        with (
            patch("ai_game_player.app.ConfigStore.load", return_value=AppConfig()),
            patch("ai_game_player.app.ShellStateStore.load", return_value=ShellState()),
            patch("ai_game_player.app.RuntimeLog"),
            patch.object(Application, "refresh_windows"),
            patch.object(Application, "_poll_global_stop"),
            patch.object(Application, "_poll_background_results"),
            patch.object(Application, "_create_session_runtime") as create_runtime,
            patch("ai_game_player.app.ToolTip", side_effect=attach_tooltip),
        ):
            application = Application(root, observation_service=SimpleNamespace())
            root.geometry("1120x760+30000+30000")
            root.update()
            self.assertEqual(len(tooltips), 1)
            tooltip = tooltips[0]
            checkbox = application.live_execution_toggle
            self.assertIs(tooltip.widget, checkbox)
            expected = (
                "実入力を許可してから操作を実行すると、アプリは選択したゲームウィンドウへ入力を送ります。"
                "判断だけの実行では入力しません。連続実行中や処理の完了待ちは変更できません。"
            )
            for event in ("<Enter>", "<FocusIn>"):
                checkbox.event_generate(event)
                root.after(30, root.quit)
                root.mainloop()
                self.assertIsNotNone(tooltip._tip_window)
                self.assertEqual(
                    tooltip._tip_window.winfo_children()[0].cget("text"), expected
                )
                self.assertFalse(application.live_execution.get())
                checkbox.event_generate("<Leave>")
                self.assertIsNone(tooltip._tip_window)
            checkbox.invoke()
            self.assertTrue(application.live_execution.get())
            original_controller = application.session_controller
            try:
                for is_looping, has_pending_step in ((True, False), (False, True)):
                    application.session_controller = SimpleNamespace(
                        is_looping=is_looping, has_pending_step=has_pending_step
                    )
                    application._refresh_execution_controls()
                    self.assertTrue(checkbox.instate(["disabled"]))
            finally:
                application.session_controller = original_controller
                application._refresh_execution_controls()
            self.assertTrue(checkbox.instate(["!disabled"]))
            create_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
