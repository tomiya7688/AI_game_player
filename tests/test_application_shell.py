import tempfile
import os
import tkinter as tk
from tkinter import ttk
import unittest
from pathlib import Path
from tkinter import TclError
from types import SimpleNamespace
from unittest.mock import patch

from ai_game_player.ui.shell import (
    WORKSPACES,
    ApplicationShell,
    ShellController,
    ShellState,
    ShellStateStore,
    ToolTip,
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
        self.assertEqual(saved, [ShellState("Play", False, True), ShellState("Play", False, False)])
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
            shell = ApplicationShell(root, ShellState(), lambda: stopped.append(True), changed.append)
            shell.pack(fill=tk.BOTH, expand=True)
            root.update_idletasks()

            self.assertEqual(tuple(shell.workspace_pages), WORKSPACES)
            self.assertEqual(shell.player_seat_host.winfo_manager(), "pack")
            self.assertEqual(shell.latest_action_label.winfo_manager(), "pack")
            self.assertEqual(shell.bottom_host.winfo_manager(), "pack")
            self.assertEqual(shell.latest_action_var.get(), "直近Action: まだありません")
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

            shell.toggle_inspector()
            self.assertEqual(len(shell.upper.panes()), 2)
            shell.toggle_inspector()
            self.assertEqual(shell.upper.panes()[-1], str(shell.inspector))
            shell.toggle_bottom_panel()
            self.assertEqual(len(shell.vertical.panes()), 1)
            shell.toggle_bottom_panel()
            self.assertEqual(shell.vertical.panes()[-1], str(shell.bottom_panel))

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
        for platform_name, expected_label in (("nt", "■ 停止 (Esc / F12)"), ("posix", "■ 停止 (Esc)")):
            with self.subTest(platform=platform_name):
                try:
                    root = tk.Tk()
                except TclError as exc:
                    self.skipTest(f"Tk desktop is unavailable: {exc}")
                try:
                    root.withdraw()
                    stop_calls = []
                    # ShellのOS参照だけを置き換え、テスト環境全体のos.nameは変更しない。
                    with patch("ai_game_player.ui.shell.os", SimpleNamespace(name=platform_name)):
                        shell = ApplicationShell(root, ShellState(), lambda: stop_calls.append(True), lambda state: None)
                    self.assertEqual(shell.stop_button.cget("text"), expected_label)
                    shell.stop_button.invoke()
                    self.assertEqual(stop_calls, [True])
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
            shell = ApplicationShell(root, ShellState(), lambda: stop_calls.append("shell"), lambda _s: None)
            shell.pack(fill=tk.BOTH, expand=True)
            root.update_idletasks()

            script = root.bind("<Escape>")
            self.assertEqual(script, original_escape_binding)
            root.eval(script)

            # Shell生成後も親の停止処理だけが1回呼ばれる。
            self.assertEqual(stop_calls, ["root"])
        finally:
            root.destroy()


    def test_tooltip_show_and_hide_behavior(self):
        try:
            root = tk.Tk()
        except TclError as exc:
            self.skipTest(f"Tk desktop is unavailable: {exc}")
        try:
            root.withdraw()
            button = ttk.Button(root, text="Test")
            button.pack()
            tooltip = ToolTip(button, "Help text")
            self.assertIsNone(tooltip.tip_window)

            tooltip.show_tip()
            self.assertIsNotNone(tooltip.tip_window)

            # Calling show_tip again when already showing should be a no-op
            tooltip.show_tip()
            self.assertIsNotNone(tooltip.tip_window)

            tooltip.hide_tip()
            self.assertIsNone(tooltip.tip_window)

            # Calling hide_tip when already hidden should be a no-op
            tooltip.hide_tip()
            self.assertIsNone(tooltip.tip_window)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
