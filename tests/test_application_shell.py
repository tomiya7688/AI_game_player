import tempfile
import os
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import TclError
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player.app import Application
from ai_game_player.player_character import PlayerState
from ai_game_player.ui.shell import (
    WORKSPACES,
    WORKSPACE_HINTS,
    ApplicationShell,
    ShellController,
    ShellState,
    ShellStateStore,
)


class ShellStateTests(unittest.TestCase):
    def test_default_state_opens_all_shell_panels_on_play(self):
        self.assertEqual(ShellState(), ShellState("Play", True, True))

    def test_invalid_workspace_or_visibility_is_rejected(self):
        with self.assertRaises(ValueError):
            ShellState("Unknown")
        with self.assertRaises(ValueError):
            ShellState("Play", 1, True)

    def test_shell_layout_state_round_trips_without_runtime_config(self):
        expected = ShellState("Reasoning", False, True)
        with tempfile.TemporaryDirectory() as directory:
            store = ShellStateStore(Path(directory) / "data/shell_state.json")
            store.save(expected)
            self.assertEqual(store.load(), expected)
            self.assertEqual(set(store.path.parent.iterdir()), {store.path})

    def test_missing_malformed_and_legacy_state_use_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shell_state.json"
            store = ShellStateStore(path)
            self.assertEqual(store.load(), ShellState())
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(store.load(), ShellState())
            path.write_text('{"workspace":"not-a-workspace"}', encoding="utf-8")
            self.assertEqual(store.load(), ShellState())
            path.write_text('{"workspace":"History"}', encoding="utf-8")
            self.assertEqual(store.load(), ShellState("History", True, True))


class ShellControllerTests(unittest.TestCase):
    def test_all_workspace_slots_can_be_selected_and_state_persisted(self):
        saved = []
        controller = ShellController(ShellState(), saved.append)
        for workspace in WORKSPACES:
            controller.select_workspace(workspace)
            self.assertEqual(controller.state.workspace, workspace)
        self.assertEqual(saved[-1], ShellState("Mods", True, True))
        with self.assertRaises(ValueError):
            controller.select_workspace("Runtime")

    def test_panel_toggle_reports_visibility_and_notifies_persistence(self):
        saved = []
        controller = ShellController(ShellState(), saved.append)
        self.assertFalse(controller.toggle_inspector())
        self.assertFalse(controller.state.inspector_visible)
        self.assertFalse(controller.toggle_bottom_panel())
        self.assertFalse(controller.state.bottom_panel_visible)
        self.assertEqual(
            saved, [ShellState("Play", False, True), ShellState("Play", False, False)]
        )
        self.assertTrue(controller.toggle_inspector())
        self.assertTrue(controller.toggle_bottom_panel())

    def test_selecting_current_workspace_does_not_rewrite_state(self):
        saved = []
        controller = ShellController(ShellState(), saved.append)
        controller.select_workspace("Play")
        self.assertEqual(saved, [])


class TkShellSmokeTests(unittest.TestCase):
    def test_panes_workspace_mounts_and_global_stop_are_wired(self):
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            changed = []
            stopped = []
            shell = ApplicationShell(
                root, ShellState(), lambda: stopped.append(True), changed.append
            )
            shell.pack(fill=tk.BOTH, expand=True)
            root.update_idletasks()

            self.assertEqual(tuple(shell.workspace_pages), WORKSPACES)
            self.assertEqual(shell.player_seat_host.winfo_manager(), "pack")
            self.assertEqual(
                shell.player_seat_host.winfo_parent(), str(shell.navigator)
            )
            self.assertEqual(shell.player_seat.name_var.get(), "賢者ミスク")
            self.assertIsNotNone(shell.header_icon)
            self.assertTrue(shell.set_player_state(PlayerState.THINK))
            self.assertEqual(shell.player_seat.state, PlayerState.THINK)
            self.assertEqual(shell.latest_action_label.winfo_manager(), "pack")
            self.assertEqual(shell.bottom_host.winfo_manager(), "pack")
            self.assertEqual(
                shell.latest_action_var.get(), "直近Action: まだありません"
            )
            shell.set_latest_action("move-left", "window_message / 入力しました")
            shell.set_status("連続実行中")
            self.assertEqual(
                shell.latest_action_var.get(),
                "直近Action: move-left / window_message / 入力しました",
            )
            shell.set_latest_action("", "応答にAction IDがありません")
            self.assertEqual(
                shell.latest_action_var.get(),
                "直近Action: 不明 / 応答にAction IDがありません",
            )
            self.assertEqual(len(shell.upper.panes()), 3)
            self.assertEqual(len(shell.vertical.panes()), 2)

            self.assertEqual(
                shell.inspector_toggle_button.cget("text"), "Inspectorを非表示"
            )
            self.assertEqual(
                shell.bottom_panel_toggle_button.cget("text"), "Bottom Panelを非表示"
            )

            shell.toggle_inspector()
            self.assertEqual(len(shell.upper.panes()), 2)
            self.assertEqual(
                shell.inspector_toggle_button.cget("text"), "Inspectorを表示"
            )
            shell.toggle_inspector()
            self.assertEqual(shell.upper.panes()[-1], str(shell.inspector))
            self.assertEqual(
                shell.inspector_toggle_button.cget("text"), "Inspectorを非表示"
            )

            shell.toggle_bottom_panel()
            self.assertEqual(len(shell.vertical.panes()), 1)
            self.assertEqual(
                shell.bottom_panel_toggle_button.cget("text"), "Bottom Panelを表示"
            )
            shell.toggle_bottom_panel()
            self.assertEqual(shell.vertical.panes()[-1], str(shell.bottom_panel))
            self.assertEqual(
                shell.bottom_panel_toggle_button.cget("text"), "Bottom Panelを非表示"
            )

            shell.select_workspace("History")
            shell.stop_button.invoke()
            self.assertEqual(shell.state.workspace, "History")
            self.assertEqual(changed[-1].workspace, "History")
            self.assertEqual(stopped, [True])

            expected_label = "■ 停止 (Esc / F12)" if os.name == "nt" else "■ 停止 (Esc)"
            self.assertEqual(shell.stop_button.cget("text"), expected_label)
        finally:
            root.destroy()

    def test_stop_shortcut_hint_matches_windows_monitor_availability(self):
        for platform_name, expected_label in (
            ("nt", "■ 停止 (Esc / F12)"),
            ("posix", "■ 停止 (Esc)"),
        ):
            with self.subTest(platform=platform_name):
                try:
                    root = tk.Tk()
                except TclError as exc:
                    self.skipTest(f"Tk desktop is unavailable: {exc}")
                try:
                    root.withdraw()
                    stop_calls = []
                    # ShellのOS参照だけを置き換え、テスト環境全体のos.nameは変更しない。
                    with patch(
                        "ai_game_player.ui.shell.os",
                        SimpleNamespace(name=platform_name),
                    ):
                        shell = ApplicationShell(
                            root,
                            ShellState(),
                            lambda: stop_calls.append(True),
                            lambda state: None,
                        )
                    self.assertEqual(shell.stop_button.cget("text"), expected_label)
                    shell.stop_button.invoke()
                    self.assertEqual(stop_calls, [True])
                finally:
                    root.destroy()

    def test_saved_hidden_panels_offer_show_actions_and_buttons_persist_changes(self):
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            saved_changes = []
            shell = ApplicationShell(
                root,
                ShellState(inspector_visible=False, bottom_panel_visible=False),
                lambda: None,
                saved_changes.append,
            )
            self.assertEqual(
                shell.inspector_toggle_button.cget("text"), "Inspectorを表示"
            )
            self.assertEqual(
                shell.bottom_panel_toggle_button.cget("text"), "Bottom Panelを表示"
            )
            self.assertEqual(len(shell.upper.panes()), 2)
            self.assertEqual(len(shell.vertical.panes()), 1)
            self.assertEqual(saved_changes, [])

            shell.inspector_toggle_button.invoke()
            self.assertEqual(
                shell.inspector_toggle_button.cget("text"), "Inspectorを非表示"
            )
            self.assertEqual(
                shell.bottom_panel_toggle_button.cget("text"), "Bottom Panelを表示"
            )
            self.assertEqual(shell.upper.panes()[-1], str(shell.inspector))
            self.assertEqual(
                saved_changes,
                [ShellState(inspector_visible=True, bottom_panel_visible=False)],
            )

            shell.bottom_panel_toggle_button.invoke()
            self.assertEqual(
                shell.bottom_panel_toggle_button.cget("text"), "Bottom Panelを非表示"
            )
            self.assertEqual(shell.vertical.panes()[-1], str(shell.bottom_panel))
            self.assertEqual(saved_changes[-1], ShellState())
            self.assertEqual(len(saved_changes), 2)
        finally:
            root.destroy()

    def test_stop_tooltip_displays_platform_conditions_and_preserves_stop_callback(
        self,
    ):
        for platform_name in ("nt", "posix"):
            with self.subTest(platform=platform_name):
                try:
                    root = tk.Tk()
                except TclError as exc:
                    self.skipTest(f"Tk desktop is unavailable: {exc}")
                try:
                    root.geometry("1100x700+30000+30000")
                    stop_calls = []
                    with patch(
                        "ai_game_player.ui.shell.os",
                        SimpleNamespace(name=platform_name),
                    ):
                        shell = ApplicationShell(
                            root,
                            ShellState(),
                            lambda: stop_calls.append(True),
                            lambda _state: None,
                        )
                    shell.pack(fill=tk.BOTH, expand=True)
                    root.update()
                    shell.stop_button.event_generate("<FocusIn>")
                    root.after(450, root.quit)
                    root.mainloop()
                    popup = next(
                        child
                        for child in shell.stop_button.winfo_children()
                        if isinstance(child, tk.Toplevel)
                    )
                    self.assertTrue(popup.winfo_viewable())
                    help_text = popup.winfo_children()[0].cget("text")
                    self.assertIn("本アプリにフォーカスがある状態のEsc", help_text)
                    self.assertIn("停止を要求します", help_text)
                    if platform_name == "nt":
                        self.assertIn(
                            "WindowsではF12でも停止を要求できます。", help_text
                        )
                        self.assertIn(
                            "Windowsの連続実行中は、自動操作以外のマウス移動を検出すると停止を要求します",
                            help_text,
                        )
                    else:
                        self.assertNotIn("F12", help_text)
                        self.assertNotIn("マウス移動", help_text)
                    shell.stop_button.event_generate("<FocusOut>")
                    self.assertFalse(popup.winfo_exists())
                    shell.stop_button.invoke()
                    self.assertEqual(stop_calls, [True])
                finally:
                    root.destroy()

    def test_workspace_buttons_have_tooltips(self):
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.geometry("1100x700+30000+30000")
            shell = ApplicationShell(root, ShellState(), lambda: None, lambda _s: None)
            shell.pack(fill=tk.BOTH, expand=True)
            root.update()
            for workspace in WORKSPACES:
                button = shell.workspace_buttons[workspace]
                button.event_generate("<FocusIn>")
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
                self.assertIsNotNone(
                    popup, f"ToolTip popup missing for workspace {workspace}"
                )
                self.assertTrue(popup.winfo_viewable())
                help_text = popup.winfo_children()[0].cget("text")
                self.assertEqual(help_text, WORKSPACE_HINTS[workspace])
                button.event_generate("<FocusOut>")
        finally:
            root.destroy()

    def test_application_shell_does_not_register_duplicate_escape_binding(self):
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            stop_calls = []
            # Applicationが所有する停止bindingをShellが変更しないことを調べる。
            # 引数なしのTclコマンドを登録し、物理キー配送やTkのイベント置換に依存しない。
            root_stop_command = root.register(lambda: stop_calls.append("root"))
            root.bind("<Escape>", root_stop_command)
            original_escape_binding = root.bind("<Escape>")
            shell = ApplicationShell(
                root, ShellState(), lambda: stop_calls.append("shell"), lambda _s: None
            )
            shell.pack(fill=tk.BOTH, expand=True)
            root.update_idletasks()

            script = root.bind("<Escape>")
            self.assertEqual(script, original_escape_binding)
            root.eval(script)

            # Shell生成後も親の停止処理だけが1回呼ばれる。
            self.assertEqual(stop_calls, ["root"])
        finally:
            root.destroy()


class ApplicationPlayerStateTests(unittest.TestCase):
    def test_player_state_is_forwarded_only_when_shell_has_a_seat(self):
        application = Application.__new__(Application)
        shown = []
        application.shell = SimpleNamespace(set_player_state=shown.append)
        application._show_player_state(PlayerState.ACT)
        self.assertEqual(shown, [PlayerState.ACT])
        application.shell = SimpleNamespace()
        application._show_player_state(PlayerState.STOP)
        self.assertEqual(shown, [PlayerState.ACT])


if __name__ == "__main__":
    unittest.main()
