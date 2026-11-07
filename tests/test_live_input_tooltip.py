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
            tooltip = ToolTip(widget, text)
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
            self.assertEqual(len(tooltips), 6)
            refresh_tooltip = tooltips[0]
            tooltip = tooltips[1]
            step_tooltip = tooltips[2]
            execute_tooltip = tooltips[3]
            loop_tooltip = tooltips[4]
            resume_tooltip = tooltips[5]
            self.assertIs(refresh_tooltip.widget, application.refresh_windows_button)
            self.assertEqual(refresh_tooltip.delay_ms, 400)
            self.assertEqual(
                refresh_tooltip.text,
                "起動中のウィンドウ一覧を取得・更新します。対象ウィンドウを起動または再起動した後に押してください。",
            )
            self.assertIs(step_tooltip.widget, application.step_button)
            self.assertEqual(step_tooltip.delay_ms, 400)
            self.assertEqual(
                step_tooltip.text,
                "画面と候補から次の操作を1回だけ判断します。ゲームウィンドウへの実入力は行いません。",
            )
            self.assertIs(execute_tooltip.widget, application.execute_button)
            self.assertEqual(execute_tooltip.delay_ms, 400)
            self.assertEqual(
                execute_tooltip.text,
                "次の操作を1回判断して実行します。「実入力を許可」のチェック状態に応じて、dry-runまたは実際の操作入力を送信します。",
            )
            self.assertIs(loop_tooltip.widget, application.loop_button)
            self.assertEqual(loop_tooltip.delay_ms, 400)
            self.assertEqual(
                loop_tooltip.text,
                "画面観測・判断・実行のループを連続して行います。停止ボタン、Escキー、またはF12キーでいつでも停止できます。",
            )
            self.assertIs(resume_tooltip.widget, application.resume_button)
            self.assertEqual(resume_tooltip.delay_ms, 400)
            self.assertEqual(
                resume_tooltip.text,
                "停止状態を解除して実行世代を更新（re-arm）し、新しい判断や連続実行を受け付け可能な状態に戻します。"
                "処理の終了待ち中は再開できません。ゲーム操作は自動では始まりません。",
            )
            checkbox = application.live_execution_toggle
            self.assertIs(tooltip.widget, checkbox)
            self.assertEqual(tooltip.delay_ms, 400)
            expected = (
                "実入力を許可してから操作を実行すると、アプリは選択したゲームウィンドウへ入力を送ります。"
                "判断だけの実行では入力しません。連続実行中や処理の完了待ちは変更できません。"
            )
            for event, end_event in (
                ("<Enter>", "<Leave>"),
                ("<FocusIn>", "<FocusOut>"),
            ):
                checkbox.event_generate(event)
                root.after(450, root.quit)
                root.mainloop()
                self.assertIsNotNone(tooltip._tip_window)
                self.assertEqual(
                    tooltip._tip_window.winfo_children()[0].cget("text"), expected
                )
                self.assertFalse(application.live_execution.get())
                checkbox.event_generate(end_event)
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
                    checkbox.event_generate("<Enter>")
                    root.after(450, root.quit)
                    root.mainloop()
                    self.assertIsNotNone(tooltip._tip_window)
                    self.assertEqual(
                        tooltip._tip_window.winfo_children()[0].cget("text"), expected
                    )
                    checkbox.event_generate("<Leave>")
                    self.assertIsNone(tooltip._tip_window)
            finally:
                application.session_controller = original_controller
                application._refresh_execution_controls()
            self.assertTrue(checkbox.instate(["!disabled"]))
            create_runtime.assert_not_called()

    def test_refresh_windows_tooltip_shows_on_hover_and_focus(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk desktop is unavailable: {error}")
        self.addCleanup(root.destroy)
        with (
            patch("ai_game_player.app.ConfigStore.load", return_value=AppConfig()),
            patch("ai_game_player.app.ShellStateStore.load", return_value=ShellState()),
            patch("ai_game_player.app.RuntimeLog"),
            patch.object(Application, "refresh_windows"),
            patch.object(Application, "_poll_global_stop"),
            patch.object(Application, "_poll_background_results"),
        ):
            application = Application(root, observation_service=SimpleNamespace())
            root.geometry("1120x760+30000+30000")
            root.update()
            button = application.refresh_windows_button
            expected = "起動中のウィンドウ一覧を取得・更新します。対象ウィンドウを起動または再起動した後に押してください。"

            for event, end_event in (
                ("<Enter>", "<Leave>"),
                ("<FocusIn>", "<FocusOut>"),
            ):
                button.event_generate(event)
                root.after(450, root.quit)
                root.mainloop()
                popup = next(
                    (
                        child
                        for child in button.winfo_children()
                        if isinstance(child, tk.Toplevel)
                    ),
                    None,
                )
                self.assertIsNotNone(popup)
                self.assertTrue(popup.winfo_viewable())
                self.assertEqual(popup.winfo_children()[0].cget("text"), expected)
                button.event_generate(end_event)
                self.assertFalse(popup.winfo_exists())

    def test_action_control_tooltips_show_on_hover_and_focus(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk desktop is unavailable: {error}")
        self.addCleanup(root.destroy)
        with (
            patch("ai_game_player.app.ConfigStore.load", return_value=AppConfig()),
            patch("ai_game_player.app.ShellStateStore.load", return_value=ShellState()),
            patch("ai_game_player.app.RuntimeLog"),
            patch.object(Application, "refresh_windows"),
            patch.object(Application, "_poll_global_stop"),
            patch.object(Application, "_poll_background_results"),
        ):
            application = Application(root, observation_service=SimpleNamespace())
            root.geometry("1120x960+30000+30000")
            root.update()
            controls_and_texts = (
                (
                    application.step_button,
                    "画面と候補から次の操作を1回だけ判断します。ゲームウィンドウへの実入力は行いません。",
                ),
                (
                    application.execute_button,
                    "次の操作を1回判断して実行します。「実入力を許可」のチェック状態に応じて、dry-runまたは実際の操作入力を送信します。",
                ),
                (
                    application.loop_button,
                    "画面観測・判断・実行のループを連続して行います。停止ボタン、Escキー、またはF12キーでいつでも停止できます。",
                ),
            )

            for button, expected in controls_and_texts:
                for event, end_event in (
                    ("<Enter>", "<Leave>"),
                    ("<FocusIn>", "<FocusOut>"),
                ):
                    button.event_generate(event)
                    root.after(450, root.quit)
                    root.mainloop()
                    popup = next(
                        (
                            child
                            for child in button.winfo_children()
                            if isinstance(child, tk.Toplevel)
                        ),
                        None,
                    )
                    self.assertIsNotNone(popup)
                    self.assertTrue(popup.winfo_viewable())
                    self.assertEqual(popup.winfo_children()[0].cget("text"), expected)
                    button.event_generate(end_event)
                    self.assertFalse(popup.winfo_exists())

    def test_resume_button_tooltip_shows_on_hover_and_focus(self):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk desktop is unavailable: {error}")
        self.addCleanup(root.destroy)
        with (
            patch("ai_game_player.app.ConfigStore.load", return_value=AppConfig()),
            patch("ai_game_player.app.ShellStateStore.load", return_value=ShellState()),
            patch("ai_game_player.app.RuntimeLog"),
            patch.object(Application, "refresh_windows"),
            patch.object(Application, "_poll_global_stop"),
            patch.object(Application, "_poll_background_results"),
        ):
            application = Application(root, observation_service=SimpleNamespace())
            root.geometry("1120x960+30000+30000")
            root.update()
            button = application.resume_button
            expected = (
                "停止状態を解除して実行世代を更新（re-arm）し、新しい判断や連続実行を受け付け可能な状態に戻します。"
                "処理の終了待ち中は再開できません。ゲーム操作は自動では始まりません。"
            )

            for event, end_event in (
                ("<Enter>", "<Leave>"),
                ("<FocusIn>", "<FocusOut>"),
            ):
                button.event_generate(event)
                root.after(450, root.quit)
                root.mainloop()
                popup = next(
                    (
                        child
                        for child in button.winfo_children()
                        if isinstance(child, tk.Toplevel)
                    ),
                    None,
                )
                self.assertIsNotNone(popup)
                self.assertTrue(popup.winfo_viewable())
                self.assertEqual(popup.winfo_children()[0].cget("text"), expected)
                button.event_generate(end_event)
                self.assertFalse(popup.winfo_exists())


if __name__ == "__main__":
    unittest.main()
